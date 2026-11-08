"""The player's Civilization V folder in Documents: reading mod descriptions, installing mods into MODS, writing
Bazaar's plugin order, and copying aside and putting back everything a check changes there. Nothing here needs the
game, so all of it works with the game closed."""

import collections
import contextlib
import ctypes
import filecmp
import glob
import json
import msvcrt
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from xml.etree import ElementTree

from . import CheckError

ORDER_FILE = os.path.join("ModUserData", "Bazaar plugin order.txt")
SAVES = os.path.join("ModdedSaves", "single")
AUTOSAVES = os.path.join(SAVES, "auto")
CONFIG = "config.ini"
GRAPHICS = "GraphicsSettingsDX11.ini"
# Where the game keeps which mods are switched on, in its Mods table.
MODS_DATABASE = os.path.join("cache", "Civ5ModsDatabase.db")
SEED_KEYS = ("SyncRandSeed", "MapRandSeed")
FULL_SCREEN = "FullScreen"
SETTING_LINE = r"(?im)^[ \t]*%s[ \t]*=[^\r\n]*"
# Of a settings file changed after a run stopped, only the lines the run sets go back: the rest may be the player's.
RUN_LINES = {CONFIG: ("seed lines", SEED_KEYS), GRAPHICS: ("FullScreen line", (FULL_SCREEN,))}
CSIDL_PERSONAL = 5
TAR = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tar.exe")
# The top of the range a mod's dependency on any version of another allows.
ANY_VERSION = 999
# Bazaar's test builds, and any mod at this version or above: above every release, and below ANY_VERSION.
TEST_VERSION = 900
COPIES_PREFIX = "Bazaar check copies "
WHERE_THEY_GO = "where these go.txt"
MANIFEST = "copies.json"
MANIFEST_VERSION = 1
HEARTBEAT = "last seen"
HANDED_OVER = "handed over"
HAND_BACK = "hand back"
RUN_LOCK = "Bazaar check running"
# A run changes files within seconds of its last mark; later changes count as the player's, even a game's own after
# its run was stopped, which is why the Mods menu choices and the lines in RUN_LINES go back whatever their age.
SINCE_MARGIN = 30
ORDER_KEYS = {"file", "skip", "use_both_with"}

ModInfo = collections.namedtuple("ModInfo", "id version name dependencies")
Dependency = collections.namedtuple("Dependency", "id title low high")
Copies = collections.namedtuple("Copies", "folder items")
Item = collections.namedtuple("Item", "label rel folder held_by_game group copy")


def user_folder():
    """The game's folder in Documents, wherever Windows keeps Documents."""
    path = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_PERSONAL, None, 0, path) != 0:
        raise CheckError("Windows did not report your Documents folder")
    return os.path.join(path.value, "My Games", "Sid Meier's Civilization 5")


def read_modinfo(folder):
    """The folder's one mod description, or None when it has none or several."""
    modinfos = glob.glob(os.path.join(glob.escape(folder), "*.modinfo"))
    if len(modinfos) != 1:
        return None
    try:
        root = ElementTree.parse(modinfos[0]).getroot()
        dependencies = [
            Dependency(
                m.get("id"),
                m.get("title") or m.get("id"),
                int(m.get("minversion", 0)),
                int(m.get("maxversion", ANY_VERSION)),
            )
            for m in root.findall("Dependencies/Mod")
        ]
        return ModInfo(root.get("id"), int(root.get("version")), root.findtext("Properties/Name") or "", dependencies)
    except (ElementTree.ParseError, TypeError, ValueError):
        raise CheckError("%s is not a mod description the game can read" % modinfos[0])


def mod_key(folder):
    info = read_modinfo(folder)
    return (info.id, info.version) if info else None


def mod_folders(mods_folder, key):
    """A broken mod of someone else's must not stop the check."""
    found = []
    for name in sorted(os.listdir(mods_folder)):
        folder = os.path.join(mods_folder, name)
        try:
            if os.path.isdir(folder) and mod_key(folder) == key:
                found.append(folder)
        except CheckError:
            pass
    return found


def different_files(expected, folder):
    """The files under expected that folder lacks or holds with other bytes."""
    different = []
    for dirpath, _, filenames in os.walk(expected):
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), expected)
            there = os.path.join(folder, rel)
            if not os.path.isfile(there) or not filecmp.cmp(os.path.join(expected, rel), there, shallow=False):
                different.append(rel)
    return sorted(different)


def in_place(folder, target):
    return os.path.exists(target) and os.path.samefile(folder, target)


def unpack(package, folder):
    os.makedirs(folder)
    if subprocess.run([TAR, "-x", "-f", package, "-C", folder], capture_output=True).returncode != 0:
        raise CheckError("%s could not be unpacked" % package)
    return folder


def order_entry_problem(entry):
    """An entry is a DLL's name, or a {file, skip, use_both_with} table; returns what is wrong with it, or None."""
    if isinstance(entry, str):
        return None
    if not isinstance(entry, dict) or type(entry.get("file")) is not str:
        return "it is neither a DLL's name nor a table with a file"
    unknown = set(entry) - ORDER_KEYS
    if unknown:
        return "it does not take %s" % ", ".join(sorted(unknown))
    if type(entry.get("skip", False)) is not bool:
        return "skip is not true or false"
    partners = entry.get("use_both_with", [])
    if type(partners) is not list or not all(isinstance(f, str) for f in partners):
        return "use_both_with is not a list of DLL names"
    return None


def order_line(entry):
    if isinstance(entry, str):
        entry = {"file": entry}
    fields = ["skip" if entry.get("skip") else "load", entry["file"], "", ""] + entry.get("use_both_with", [])
    return "\t".join(fields) + "\r\n"


def and_list(names):
    return ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else "".join(names)


def save_rel(name):
    return os.path.join(SAVES, name + ".Civ5Save")


def items_to_keep(saves=(), mods=(), graphics=False):
    return [
        Item("Mods menu choices", MODS_DATABASE, False, True, "", None),
        Item("plugin order", ORDER_FILE, False, False, "", None),
        Item("config.ini", CONFIG, False, True, "", None),
        *([Item(GRAPHICS, GRAPHICS, False, True, "", None)] if graphics else []),
        Item("autosaves", AUTOSAVES, True, True, "", None),
        *[Item('save "%s"' % name, save_rel(name), False, False, "saves", None) for name in saves],
        *[Item("mod folder %s" % name, os.path.join("MODS", name), True, True, "mods", None) for name in mods],
    ]


def copied_text(copies):
    return and_list([item.label for item in copies.items if item.copy])


def remove_tree(path):
    """shutil.rmtree, read-only files included, which Windows will not otherwise delete."""

    def clear(function, failed, _):
        os.chmod(failed, stat.S_IWRITE)
        function(failed)

    shutil.rmtree(path, **({"onexc": clear} if sys.version_info >= (3, 12) else {"onerror": clear}))


def replace_file(copy, path):
    """Written beside the file first, so a failed write leaves it whole."""
    try:
        shutil.copy2(copy, path + ".bazaar-check")
        os.replace(path + ".bazaar-check", path)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(path + ".bazaar-check")
        raise


@contextlib.contextmanager
def one_run_at_a_time():
    """Held for a whole run: two at once would each copy aside, as the player's own, what the other changed. A
    second take is refused even in the same process."""
    path = os.path.join(tempfile.gettempdir(), RUN_LOCK)
    held = open(path, "a+b")
    try:
        held.seek(0)
        msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        held.close()
        raise CheckError("another check is running: wait for it to end")
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            held.seek(0)
            msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 1)
        held.close()
        with contextlib.suppress(OSError):
            os.remove(path)


def copies_folders():
    pattern = os.path.join(glob.escape(tempfile.gettempdir()), glob.escape(COPIES_PREFIX) + "*")
    return sorted(glob.glob(pattern))


def hand_back(timeout=10):
    for folder in copies_folders():
        if not os.path.isfile(os.path.join(folder, HANDED_OVER)):
            continue
        asked = os.path.join(folder, HAND_BACK)
        try:
            open(asked, "wb").close()
        except OSError:
            continue
        # The waiting run deletes the file as it takes the game back; a run killed during its hand-over never will.
        deadline = time.time() + timeout
        while os.path.exists(asked) and time.time() < deadline:
            time.sleep(0.2)
        if not os.path.exists(asked):
            return True
        with contextlib.suppress(OSError):
            os.remove(asked)
    return False


def finished_copying(folder):
    """The note is written last and deleted first: a folder without it was left by a run stopped while copying,
    before it changed anything, or by one that put everything back but could not delete all of the folder."""
    return os.path.isfile(os.path.join(folder, WHERE_THEY_GO))


def delete_copies(folder):
    """Why the copies folder could not be deleted, or None. The note goes first, so what is left is no leftover."""
    try:
        for name in (WHERE_THEY_GO, MANIFEST):
            os.remove(os.path.join(folder, name))
        remove_tree(folder)
    except OSError as e:
        return "your files are back, but %s could not be deleted (%s): delete it yourself" % (folder, e)
    return None


def by_hand(folder):
    return CheckError(
        "copies an earlier check made are still in %s, and it cannot put them back itself: if that check did not put"
        ' your files back, put them back as "%s" there says; then delete the folder' % (folder, WHERE_THEY_GO)
    )


def refuse_leftover_copies():
    """Copying the files an earlier run's copies replaced would keep that run's changes for good."""
    leftover = [folder for folder in copies_folders() if finished_copying(folder)]
    if leftover:
        raise by_hand(leftover[0])


class PlayerFiles:
    def __init__(self, user, say=print):
        self.user = user
        self.say = say
        self.running = None

    def path(self, *parts):
        return os.path.join(self.user, *parts)

    def stage(self, source, staging):
        """The folder holding the mod, unpacked into staging if it is a package, and its folder in MODS. A package
        goes into MODS under the name the game would unpack it to."""
        if not os.path.exists(source):
            raise CheckError("%s does not exist" % source)
        packaged = os.path.isfile(source)
        folder = unpack(source, os.path.join(staging, os.path.basename(source))) if packaged else source
        info = read_modinfo(folder)
        if info is None:
            raise CheckError("%s holds no mod description, or more than one" % source)
        if packaged and re.search(r'[\\/:*?"<>|]', info.name):
            raise CheckError("%s names its mod %s, which cannot be a folder name" % (source, info.name))
        target = self.path("MODS", "%s (v %d)" % (info.name, info.version) if packaged else os.path.basename(folder))
        if os.path.exists(target):
            there = read_modinfo(target)
            if there is None or there.id != info.id:
                raise CheckError("%s already holds a different mod: move it out of MODS first" % target)
        return folder, target

    def stage_all(self, sources, staging):
        """Every source is read before anything is copied, and a folder in MODS may be replaced only by the same
        mod."""
        staged = [self.stage(source, staging) for source in sources]
        targets = [os.path.normcase(target) for _, target in staged]
        if len(set(targets)) != len(targets):
            raise CheckError("mods lists two that go into the same folder in MODS: keep one")
        return staged

    def changed_by(self, staged):
        """The folders in MODS that installing would replace or add: not a mod checked where it is."""
        return [os.path.basename(target) for folder, target in staged if not in_place(folder, target)]

    def install(self, staged):
        """Returns the folders holding what the game should load."""
        for folder, target in staged:
            if in_place(folder, target):
                continue
            if os.path.exists(target):
                remove_tree(target)
            shutil.copytree(folder, target)
        return [folder for folder, _ in staged]

    def installed_problems(self, expected_folders):
        """Why the game might not load exactly these mods: another copy of the same mod and version in MODS, such as
        a test build or a Workshop download, or a copy that differs from what was installed."""
        problems = []
        for expected in expected_folders:
            info = read_modinfo(expected)
            folders = mod_folders(self.path("MODS"), (info.id, info.version))
            if not folders:
                problems.append("MODS holds no copy of %s (v %d)" % (info.name, info.version))
                continue
            if len(folders) > 1:
                problems.append(
                    "MODS holds %d copies of %s (v %d): %s; move the others out of MODS"
                    % (len(folders), info.name, info.version, ", ".join(folders))
                )
                continue
            changed = different_files(expected, folders[0])
            if changed:
                problems.append(
                    "%s is not the build in %s (%s differ): something changed it after the check put it there, such as"
                    " a Workshop download or a .civ5mod package of the same mod in MODS; move that out of MODS"
                    % (folders[0], os.path.basename(expected), ", ".join(changed[:5]))
                )
        return problems

    def copy_item(self, item, folder):
        """The item with where its copy is in folder; unchanged, without a copy, when the player has none."""
        path = self.path(item.rel)
        if not (os.path.isdir(path) if item.folder else os.path.isfile(path)):
            return item
        copy = os.path.join(item.group, os.path.basename(item.rel))
        os.makedirs(os.path.dirname(os.path.join(folder, copy)), exist_ok=True)
        if item.folder:
            shutil.copytree(path, os.path.join(folder, copy))
        else:
            shutil.copy2(path, os.path.join(folder, copy))
        return item._replace(copy=copy)

    def put_back_item(self, folder, item):
        """Why the item could not be put back as its copy holds it, or removed when the player had none; or None."""
        path = self.path(item.rel)
        copy = os.path.join(folder, item.copy) if item.copy else None
        try:
            if copy and not os.path.exists(copy):
                raise OSError("its copy %s is missing" % copy)
            if item.rel == MODS_DATABASE:
                # A journal left by a game that died mid-write would be replayed into the restored database.
                for journal in (path + "-journal", path + "-wal", path + "-shm"):
                    if os.path.exists(journal):
                        os.remove(journal)
            if item.folder:
                if os.path.isdir(path):
                    remove_tree(path)
                if copy:
                    shutil.copytree(copy, path)
            elif copy is None:
                if os.path.exists(path):
                    os.remove(path)
            elif not (os.path.isfile(path) and filecmp.cmp(copy, path, shallow=False)):
                replace_file(copy, path)
        except OSError as e:
            return "your %s could not be put back (%s)" % (item.label, e)
        return None

    def put_back_lines(self, rel, copy):
        """Why the lines a run sets in this settings file could not be put back as its copy holds them, or None."""
        what, keys = RUN_LINES[rel]
        try:
            with open(copy, "rb") as f:
                old = f.read().decode("latin-1")
            self.set_lines(rel, {key: m.group(0) for key in keys if (m := re.search(SETTING_LINE % key, old))})
        except (OSError, CheckError) as e:
            return "the %s in your %s could not be put back (%s)" % (what, rel, e)
        return None

    def changed_since(self, item, since):
        path = self.path(item.rel)
        if not os.path.exists(path):
            return False
        times = [os.path.getmtime(path)]
        if item.folder:
            for folder, _, names in os.walk(path):
                times += [os.path.getmtime(os.path.join(folder, name)) for name in names]
        return max(times) > since + SINCE_MARGIN

    def where_they_go(self, copies):
        lines = [
            "These are copies a Bazaar check made before it changed your files. If it could not put them back, close",
            "Civilization V, then:",
        ]
        for item in copies.items:
            path = self.path(item.rel)
            if item.copy is None:
                lines.append(
                    "- your %s: delete %s if it is there: it was not there before the check" % (item.label, path)
                )
            elif item.folder:
                lines.append("- your %s: put the folder %s in place of %s" % (item.label, item.copy, path))
            else:
                lines.append("- your %s: copy %s into %s" % (item.label, item.copy, os.path.dirname(path)))
        lines.append("Then delete this folder.")
        return "\r\n".join(lines) + "\r\n"

    def copy_aside(self, saves=(), mods=(), graphics=False):
        """Copies, on disk so they survive the script being killed, of everything a run changes: the Mods menu's
        choices, the plugin order, config.ini, with graphics the game's display settings, the autosaves, which the game
        rotates whenever a new game starts, the saves of these names, and these folders in MODS. Its manifest lets a
        later run put the copies back."""
        refuse_leftover_copies()
        folder = tempfile.mkdtemp(prefix=COPIES_PREFIX)
        self.running = folder
        try:
            open(os.path.join(folder, HEARTBEAT), "wb").close()
            copies = Copies(folder, [self.copy_item(item, folder) for item in items_to_keep(saves, mods, graphics)])
            manifest = {
                "version": MANIFEST_VERSION,
                "user": self.user,
                "started": time.strftime("%Y-%m-%d %H:%M"),
                "items": [item._asdict() for item in copies.items],
            }
            with open(os.path.join(folder, MANIFEST), "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=1)
            with open(os.path.join(folder, WHERE_THEY_GO), "w", encoding="utf-8", newline="") as f:
                f.write(self.where_they_go(copies))
        except BaseException:
            self.running = None
            with contextlib.suppress(OSError):
                remove_tree(folder)
            raise
        if copied_text(copies):
            self.say("copies of your %s are kept in %s until this run ends" % (copied_text(copies), folder))
        return copies

    def beat(self):
        """Mark the run as still going: a later run takes what changed well after the last mark for the player's."""
        if self.running:
            with contextlib.suppress(OSError):
                os.utime(os.path.join(self.running, HEARTBEAT))

    def begin_hand_over(self):
        with contextlib.suppress(OSError):
            os.remove(os.path.join(self.running, HAND_BACK))
        open(os.path.join(self.running, HANDED_OVER), "wb").close()

    def handed_back(self):
        return os.path.isfile(os.path.join(self.running, HAND_BACK))

    def end_hand_over(self):
        for name in (HANDED_OVER, HAND_BACK):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(self.running, name))

    def read_copies(self, folder):
        """The copies a run left in folder, as copy_aside made them, and when that run started."""
        try:
            with open(os.path.join(folder, MANIFEST), encoding="utf-8") as f:
                manifest = json.load(f)
            if manifest["version"] != MANIFEST_VERSION:
                raise ValueError("another version")
            user, started = manifest["user"], manifest["started"]
            items = [Item(**entry) for entry in manifest["items"]]
        except (OSError, ValueError, KeyError, TypeError):
            raise by_hand(folder)
        if os.path.normcase(os.path.abspath(user)) != os.path.normcase(os.path.abspath(self.user)):
            raise CheckError(
                "copies an earlier check made of %s are still in %s: check that folder, or delete the copies"
                % (user, folder)
            )
        if not os.path.isfile(os.path.join(folder, HEARTBEAT)):
            raise by_hand(folder)
        if any(item.copy and not os.path.exists(os.path.join(folder, item.copy)) for item in items):
            raise by_hand(folder)
        return Copies(folder, items), started

    def put_back_leftovers(self):
        """Put back what a run, stopped before it could, kept copies of, so that it is never taken for the player's
        own; but leave what changed after the run stopped, which may be the player's own since. Only with the game
        closed and one_run_at_a_time held, since another run's copies folder is no leftover."""
        for folder in copies_folders():
            if not finished_copying(folder):
                with contextlib.suppress(OSError):
                    remove_tree(folder)
                continue
            copies, started = self.read_copies(folder)
            since = os.path.getmtime(os.path.join(folder, HEARTBEAT))
            mods_changed = any(item.rel == MODS_DATABASE and self.changed_since(item, since) for item in copies.items)
            problems, newer = self.restore(copies, since=since)
            if problems:
                raise CheckError(
                    "a check left copies of your files, and putting them back failed: %s" % "; ".join(problems)
                )
            left = ", except what changed after it stopped, which stays as it is: %s" % and_list(newer) if newer else ""
            if mods_changed:
                left += "; your Mods menu choices went back to how they were before it: switch on again any mods you"
                left += " switched on since"
            self.say("a check started on %s had not put your files back: they are back now%s" % (started, left))

    def restore(self, copies, autosaves_touched=True, game_running=False, since=None):
        """Put the items back; returns what could not be, which keeps the copies, and the items left as they are
        because they changed after since. The game holds some items while it runs, so those wait until it has
        closed."""
        problems, waiting, newer = [], [], []
        for item in copies.items:
            if item.rel == AUTOSAVES and not autosaves_touched:
                continue
            if game_running and item.held_by_game:
                waiting.append(item.label)
            elif since is not None and item.rel != MODS_DATABASE and self.changed_since(item, since):
                if item.rel in RUN_LINES and item.copy:
                    newer.append("%s except its %s" % (item.label, RUN_LINES[item.rel][0]))
                    problems.append(self.put_back_lines(item.rel, os.path.join(copies.folder, item.copy)))
                else:
                    newer.append(item.label)
            else:
                problems.append(self.put_back_item(copies.folder, item))
        if waiting:
            problems.append("Civilization V is still running, so these wait to be put back: %s" % and_list(waiting))
        problems = [problem for problem in problems if problem]
        if problems:
            return problems + [
                'the copies stay in %s, and "%s" there says where each goes' % (copies.folder, WHERE_THEY_GO)
            ], newer
        problem = delete_copies(copies.folder)
        return [problem] if problem else [], newer

    def put_back(self, copies, autosaves_touched=True, game_running=False):
        """Returns what could not be put back; the copies are deleted unless something was not."""
        return self.restore(copies, autosaves_touched, game_running)[0]

    def set_lines(self, rel, lines):
        """Replace these lines of the settings file, each found by its key."""
        path = self.path(rel)
        try:
            with open(path, "rb") as f:
                text = f.read().decode("latin-1")
        except FileNotFoundError:
            text = ""
        for key, line in lines.items():
            text, found = re.subn(SETTING_LINE % key, lambda _, line=line: line, text)
            if not found:
                raise CheckError(
                    "%s has no %s line: start Civilization V once, so it writes the file in full" % (path, key)
                )
        with open(path, "wb") as f:
            f.write(text.encode("latin-1"))

    def use_seed(self, seed):
        """The game reads both its seeds from config.ini as it starts; 0 has it take them from the clock."""
        self.set_lines(CONFIG, {key: "%s = %d" % (key, seed) for key in SEED_KEYS})

    def use_windowed(self):
        """A full-screen game in the background stops answering, and nobody is at the keyboard to bring it back."""
        self.set_lines(GRAPHICS, {FULL_SCREEN: FULL_SCREEN + " = 0"})

    def use_plugin_order(self, entries):
        """Bazaar loads every listed plugin, in its own order, when the order file is empty."""
        path = self.path(ORDER_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("".join(order_line(entry) for entry in entries))

    def log(self, name, since):
        """The log's lines if the game wrote it since then. Bazaar empties its log only when it first writes, so an
        older file holds a past run's lines."""
        path = self.path("Logs", name)
        if since is None or not os.path.exists(path) or os.path.getmtime(path) < since:
            return []
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
