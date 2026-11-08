"""Drive Civilization V for the in-game checks through its tuner port (tuner.py): start it through Steam, choose the
mods, start a game, end turns, save, load and quit, with nobody at the keyboard. Everything a run changes in the
player's folder is put back afterwards.

The game must be set up for it once: EnableTuner = 1 and LoggingEnabled = 1 in its config.ini, and DirectX 11 chosen
the last time it was started from Steam, since starting it through steam://rungameid starts the version chosen last.
"""

import contextlib
import csv
import ctypes
import io
import msvcrt
import os
import re
import subprocess
import sys
import time

from . import CheckError, files, tuner

GAME_URL = "steam://rungameid/8930"
DX11_EXE = "civilizationv_dx11.exe"
FRONT_END = "MainMenu"
MODS_BROWSER = "ModsBrowser"
IN_GAME = "InGame"
LOAD_SCREEN = "LoadScreen"
LEGAL_SCREEN = "LegalScreen"
NOT_BLOCKED = -1
REFUSED = "refused: "
ERRORS = (CheckError, tuner.TunerError, tuner.LuaError)
HAND_BACK_HOW = "When you are done, hand the game back: %s. Closing the game instead ends the run."
CLOSED_DURING_HAND_OVER = "Civilization V was closed during the hand-over"
LEFT_OPEN = "Ctrl+C again stops now and leaves the game open: the next run puts your files back."
LOOK_EVERY = 5


def print_now(text):
    # A program running the check reads its output through a pipe, which Python would otherwise hold back.
    print(text, flush=True)


class CarryOnWhenUnread:
    """Stands in for stdout, so that a run whose reader has gone, such as a program that ran the check and was then
    killed, still finishes: it puts everything back and ends with the exit code that says whether the checks passed."""

    def __init__(self, stream):
        self.stream = stream

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def write(self, text):
        return self._carry_on(self.stream.write, text)

    def flush(self):
        return self._carry_on(self.stream.flush)

    def _carry_on(self, do, *args):
        try:
            return do(*args)
        except OSError:
            # Windows says EINVAL here, not BrokenPipeError. Python's own flush at exit would fail too, and change the
            # exit code.
            nul = os.open(os.devnull, os.O_WRONLY)
            os.dup2(nul, self.stream.fileno())
            os.close(nul)
            return do(*args)


def make_output_safe():
    """For each program that runs checks. Piped, Python writes cp1252, and a character it lacks would end a run at the
    line that holds it, as would a reader that has gone."""
    sys.stdout.reconfigure(errors="replace")
    sys.stdout = CarryOnWhenUnread(sys.stdout)


def has_console():
    # isatty takes input from NUL, which a program running the check in the background gives it, for a console.
    try:
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
    except (AttributeError, OSError, ValueError):
        return False
    return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(ctypes.c_uint())))


def enter_pressed():
    if not has_console():
        return False
    pressed = False
    while msvcrt.kbhit():
        pressed = msvcrt.getwch() in "\r\n" or pressed
    return pressed


def hand_back_how():
    command = 'run "%s" -m bazaar_check --hand-back in the folder that holds bazaar_check' % sys.executable
    return HAND_BACK_HOW % ("press Enter here, or " + command if has_console() else command)


def config_problems(text):
    return [
        "%s = 1" % name
        for name in ("EnableTuner", "LoggingEnabled")
        if not re.search(r"^\s*%s\s*=\s*1\s*$" % name, text, re.M)
    ]


def parse_tasklist(out):
    return [
        row[0].lower() for row in csv.reader(io.StringIO(out)) if row and row[0].lower().startswith("civilizationv")
    ]


def game_processes():
    command = ["tasklist", "/FO", "CSV", "/NH", "/FI", "IMAGENAME eq CivilizationV*"]
    return parse_tasklist(subprocess.run(command, capture_output=True, text=True).stdout)


def mod_problems(wanted, enabled):
    on = {(m, v): name for m, v, name in enabled}
    problems = []
    missing = ["%s v%d" % key for key in wanted if key not in on]
    if missing:
        problems.append("not switched on: " + ", ".join(missing))
    extra = ["%s (v %d)" % (name, key[1]) for key, name in on.items() if key not in wanted]
    if extra:
        problems.append("still on: " + ", ".join(extra))
    return problems


def mod_table(mods):
    return "{%s}" % ", ".join("{%s, %d}" % (tuner.lua_text(i), v) for i, v in mods)


LIST_MODS = r"""
for _, v in pairs(Modding.GetModBrowserInstalledListings()) do
    local name = tostring(v.Title or v.Name):gsub("[\t\r\n]", " ")
    print(v.ModId .. "\t" .. v.Version .. "\t" .. tostring(v.Enabled) .. "\t" .. name)
end
"""

SET_MODS = r"""
local wanted, keep = %(mods)s, {}
for _, m in ipairs(wanted) do keep[m[1] .. "/" .. m[2]] = true end
for _, v in pairs(Modding.GetModBrowserInstalledListings()) do
    if v.Enabled and not keep[v.ModId .. "/" .. v.Version] then Modding.DisableMod(v.ModId, v.Version) end
end
for _, m in ipairs(wanted) do Modding.EnableMod(m[1], m[2]) end
"""

MOD_DESCRIPTION = r"""
local details = Modding.GetInstalledModDetails(%(id)s, %(version)d)
if details and details.Description then print(details.Description) end
"""

PLUGINS = r"""
for row in DB.Query("SELECT File, Status, Problems FROM BazaarPlugins ORDER BY Position") do
    local problems = tostring(row.Problems or ""):gsub("[\t\r\n]", " ")
    print(tostring(row.File) .. "\t" .. tostring(row.Status) .. "\t" .. problems)
end
"""

# What the game's own setup screen does from the Mods menu, which leaves the player's Play Now settings alone. The
# civilizations, difficulty, speed and era are fixed too, so with the same seeds and the same saved game options, the
# same game is played.
START_GAME = r"""
if not (GameInfo.Civilizations.CIVILIZATION_AMERICA and GameInfo.Civilizations.CIVILIZATION_ROME) then
    print("%(refused)sthe checks play America against Rome, and a mod switched on has removed one of them")
    return
end
local continents
for row in GameInfo.MapScripts() do
    if row.FileName and string.find(row.FileName, "\\Continents.lua", 1, true) then
        continents = row.FileName
        break
    end
end
if not continents then
    print("%(refused)sthe checks play on the Continents map, and the game lists no Continents map script")
    return
end
PreGame.SetPrivateGame(false)
PreGame.SetGameType(GameTypes.GAME_SINGLE_PLAYER)
PreGame.ResetSlots()
PreGame.ResetGameOptions()
PreGame.ResetMapOptions()
PreGame.LoadPreGameSettings()
PreGame.SetPersistSettings(false)
PreGame.SetCivilization(0, GameInfo.Civilizations.CIVILIZATION_AMERICA.ID)
PreGame.SetCivilization(1, GameInfo.Civilizations.CIVILIZATION_ROME.ID)
PreGame.SetHandicap(0, GameInfo.HandicapInfos.HANDICAP_PRINCE.ID)
PreGame.SetGameSpeed(GameInfo.GameSpeeds.GAMESPEED_STANDARD.ID)
PreGame.SetEra(GameInfo.Eras.ERA_ANCIENT.ID)
PreGame.SetLoadWBScenario(false)
PreGame.SetRandomMapScript(false)
PreGame.SetMapScript(continents)
local world = GameInfo.Worlds.WORLDSIZE_DUEL
PreGame.SetRandomWorldSize(false)
PreGame.SetWorldSize(world.ID)
PreGame.SetNumMinorCivs(world.DefaultMinorCivs)
Events.SerialEventStartGame()
UIManager:SetUICursor(1)
"""

CLOSE_POPUP = 'if not ContextPtr:IsHidden() then UIManager:DequeuePopup(ContextPtr) print("closed") end'

LOAD_GAME = r"""
local ending = %(ending)s
local files = {}
UI.SaveFileList(files, GameTypes.GAME_SINGLE_PLAYER, false, true)
for _, path in ipairs(files) do
    if string.sub(path, -#ending) == ending then
        UIManager:SetUICursor(1)
        Events.PlayerChoseToLoadGame(path)
        return
    end
end
print("%(refused)sthe game does not list the save " .. ending)
"""

# Ending a turn waits for every unit to have orders, every city to build something and research to be chosen, so the
# checks spend their units' moves and choose the first unit each idle city can train and the first research open.
READY_TO_END = r"""
local player = Players[Game.GetActivePlayer()]
for unit in player:Units() do unit:FinishMoves() end
for city in player:Cities() do
    if city:GetOrderQueueLength() == 0 then
        for unit in GameInfo.Units() do
            if city:CanTrain(unit.ID) then
                city:PushOrder(OrderTypes.ORDER_TRAIN, unit.ID, -1, false, false, false)
                break
            end
        end
    end
end
if player:GetCurrentResearch() == -1 then
    for tech in GameInfo.Technologies() do
        if player:CanResearch(tech.ID) then
            player:PushResearch(tech.ID, true)
            break
        end
    end
end
"""

BLOCKER = r"""
local t = Players[Game.GetActivePlayer()]:GetEndTurnBlockingType()
local name = tostring(t)
for n, v in pairs(EndTurnBlockingTypes) do if v == t then name = n end end
print(t .. "\t" .. name)
"""

# The game ignores End Turn until it has finished its own work, and a repeat is ignored once the turn has ended.
END_TURN = r"""
local turn = Game.GetGameTurn()
if turn == %(turn)d then Game.DoControl(GameInfoTypes.CONTROL_ENDTURN) end
print(tostring(turn > %(turn)d and Players[Game.GetActivePlayer()]:IsTurnActive()))
"""


class GameClosed(CheckError):
    pass


class Game:
    def __init__(self, say=print_now, port=tuner.PORT, user=None):
        self.say = say
        self.tuner = tuner.Tuner(port=port)
        self.files = files.PlayerFiles(user or files.user_folder(), say)
        self.started = None
        self.handed_over = False
        self.mods_on = False

    def path(self, *parts):
        return self.files.path(*parts)

    def lua(self, state, code, timeout=30):
        return self.tuner.run(state, code, timeout)

    def text(self, state, expression, timeout=30):
        return "\n".join(self.lua(state, "print(tostring(%s))" % expression, timeout))

    def flag(self, state, expression, timeout=30):
        return self.text(state, "(%s) and true or false" % expression, timeout) == "true"

    def state_ids(self, name):
        return self.tuner.state_ids(name)

    def wait(self, what, ready, timeout):
        """While the game loads it answers slowly or not at all, so a failed ready() only means not yet. The last
        failure is named if the wait runs out. A dropped tuner connection is made again, and the wait ends with
        GameClosed once the game has gone."""
        deadline = time.time() + timeout
        last = None
        while True:
            try:
                if ready():
                    return
            except (tuner.NoSuchState, tuner.LuaError) as e:
                last = e
            except tuner.TunerError as e:
                last = e
                if not game_processes():
                    raise GameClosed("Civilization V closed while waiting for %s" % what)
                with contextlib.suppress(tuner.TunerError):
                    self.tuner.connect(min(20, max(deadline - time.time(), 1)))
            if time.time() > deadline:
                because = " (last error: %s)" % last if last else ""
                raise CheckError("%s did not happen within %d seconds%s" % (what, timeout, because))
            self.files.beat()
            time.sleep(2)

    def preflight(self):
        """What must hold before a check changes anything."""
        if game_processes():
            raise CheckError("Civilization V is already running: close it, then run this again")
        config = self.path("config.ini")
        if not os.path.isfile(config) or not os.path.isdir(self.path("MODS")):
            raise CheckError("%s has no config.ini or MODS folder: start Civilization V once first" % self.files.user)
        with open(config, encoding="utf-8", errors="replace") as f:
            problems = config_problems(f.read())
        if problems:
            raise CheckError("%s needs %s; set that with the game closed" % (config, " and ".join(problems)))

    def start(self, timeout=300):
        self.preflight()
        self.say("starting Civilization V through Steam")
        self.started = time.time()
        os.startfile(GAME_URL)
        self.wait("Civilization V starting", game_processes, 120)
        running = game_processes()
        if not running:
            raise CheckError("Civilization V closed as it started")
        if DX11_EXE not in running:
            with contextlib.suppress(ERRORS):
                self.tuner.connect(60)
                self.wait("the main menu showing", self.menu_ready, 120)
                self.quit(60)
            raise CheckError(
                "Steam started %s, not the DirectX 11 version: start the game once from Steam choosing DirectX 11,"
                " then run this again" % ", ".join(running)
            )
        try:
            self.tuner.connect(timeout)
            self.wait("the main menu showing", self.menu_ready, timeout)
        except ERRORS:
            with contextlib.suppress(ERRORS):
                self.quit(60)
            raise

    def menu_ready(self):
        """At the main menu, after closing the legal screen that waits for a click at startup, as its button does."""
        if self.state_ids(LEGAL_SCREEN) and not self.flag(LEGAL_SCREEN, "ContextPtr:IsHidden()"):
            self.lua(LEGAL_SCREEN, "UIManager:DequeuePopup(ContextPtr)")
        return self.at_main_menu()

    def at_main_menu(self):
        return bool(self.state_ids(FRONT_END)) and not self.flag(FRONT_END, "ContextPtr:IsHidden()")

    def live_state(self):
        """The game's Lua state in a game, and the menus' outside one."""
        return IN_GAME if self.state_ids(IN_GAME) else FRONT_END

    def mods(self, enabled_only=True):
        rows = []
        for line in self.lua(FRONT_END, LIST_MODS):
            fields = line.split("\t", 3)
            if len(fields) != 4 or not fields[1].isdigit():
                raise CheckError("the game listed a mod as %r" % line)
            if fields[2] == "true" or not enabled_only:
                rows.append((fields[0], int(fields[1]), fields[3]))
        return rows

    def set_mods(self, wanted):
        self.lua(FRONT_END, SET_MODS % {"mods": mod_table(wanted)})
        problems = mod_problems(wanted, self.mods())
        if problems:
            raise CheckError("the Mods menu could not be set; " + "; ".join(problems))

    def mod_description(self, mod_id, version):
        lines = self.lua(FRONT_END, MOD_DESCRIPTION % {"id": tuner.lua_text(mod_id), "version": version})
        return "\n".join(lines) if lines else None

    def plugins(self):
        """The Plugins screen's rows as (file, status, problems)."""
        return [tuple((line.split("\t", 2) + ["", ""])[:3]) for line in self.lua(self.live_state(), PLUGINS)]

    def activate(self):
        """Switch the chosen mods on as the Mods menu's Next button does, which loads Bazaar and its plugins."""
        self.say("switching the mods on")
        self.mods_on = True
        self.lua(MODS_BROWSER, "OnNextButtonClicked()", timeout=180)

    def _through_load_screen(self, state, code, timeout):
        """Run code that starts or loads a game, then press the loading screen's button. Only states newer than those
        listed before code ran count, since the last game's stay listed for a moment."""
        screens = max(self.state_ids(LOAD_SCREEN) or [-1])
        games = max(self.state_ids(IN_GAME) or [-1])
        for line in self.lua(state, code):
            if line.startswith(REFUSED):
                raise CheckError(line[len(REFUSED) :])

        def loaded():
            ids = self.state_ids(LOAD_SCREEN)
            return bool(ids) and max(ids) > screens and not self.flag(LOAD_SCREEN, "Controls.ActivateButton:IsHidden()")

        self.wait("the game loading", loaded, timeout)
        self.lua(LOAD_SCREEN, "OnActivateButtonClicked()")
        self.wait("the game starting", lambda: max(self.state_ids(IN_GAME) or [-1]) > games, 60)

    def start_game(self, timeout=300):
        self.say("starting a game")
        self._through_load_screen(FRONT_END, START_GAME % {"refused": REFUSED}, timeout)

    def load(self, name, timeout=300):
        self.say("loading %s" % name)
        code = LOAD_GAME % {"ending": tuner.lua_text("\\" + name + ".Civ5Save"), "refused": REFUSED}
        self._through_load_screen(self.live_state(), code, timeout)

    def turn(self):
        return int(self.text(IN_GAME, "Game.GetGameTurn()"))

    def blocker(self):
        lines = self.lua(IN_GAME, BLOCKER)
        fields = lines[0].split("\t") if lines else []
        if len(fields) != 2:
            raise CheckError("the game reported what it waits for as %r" % lines)
        return int(fields[0]), fields[1]

    def close_popups(self):
        """Close the game's pop-ups, which hold a turn while they are open, as their own Close buttons do. Bazaar's
        crash pop-up, BazaarStopped, is not one, so a check can still read it."""
        closed = []
        for name in sorted({name for _, name in self.tuner.states() if name.endswith("Popup")}):
            with contextlib.suppress(tuner.NoSuchState, tuner.LuaError):
                if self.lua(name, CLOSE_POPUP) == ["closed"]:
                    closed.append(name)
        if closed:
            self.say("closed " + ", ".join(closed))
        return closed

    def end_turn(self, timeout=180):
        """End the human player's turn; returns the new turn's number."""
        turn = self.turn()
        self.lua(IN_GAME, READY_TO_END)

        def next_turn():
            if self.lua(IN_GAME, END_TURN % {"turn": turn}) == ["true"]:
                return True
            self.close_popups()
            return False

        try:
            self.wait("turn %d ready to end" % turn, lambda: self.blocker()[0] == NOT_BLOCKED, 20)
            self.wait("turn %d starting" % (turn + 1), next_turn, timeout)
        except GameClosed:
            raise
        except CheckError as e:
            waiting = "the check could not read what the game is waiting for"
            with contextlib.suppress(ERRORS):
                number, name = self.blocker()
                waiting = "the game is waiting for %s" % name
                if number == NOT_BLOCKED:
                    waiting = "nothing blocks the turn, so the other players may still be moving"
            raise CheckError("%s; %s" % (e, waiting))
        self.say("turn %d" % (turn + 1))
        return turn + 1

    def save(self, name, timeout=60):
        self.say("saving %s" % name)
        path = self.path(files.save_rel(name))
        before = os.path.getmtime(path) if os.path.exists(path) else 0
        self.lua(IN_GAME, "UI.SaveGame(%s)" % tuner.lua_text(name))

        def written():
            if not os.path.exists(path) or os.path.getmtime(path) <= before:
                return False
            size = os.path.getsize(path)
            time.sleep(1)
            return os.path.getsize(path) == size

        self.wait("the save %s" % name, written, timeout)
        return path

    def to_main_menu(self, timeout=180):
        """Leave the game for the menus. Which menu shows depends on how the game was started, so only the menus'
        Lua answering again counts."""
        if not self.state_ids(IN_GAME):
            return
        self.lua(IN_GAME, "Events.ExitToMainMenu()")
        self.wait("the menus answering", lambda: not self.state_ids(IN_GAME) and self.state_ids(FRONT_END), timeout)

    def quit(self, timeout=120):
        with contextlib.suppress(tuner.TunerError, tuner.LuaError):
            if not self.tuner.connected and game_processes():
                self.tuner.connect(10)
            self.lua(self.live_state(), "UI.ExitGame()", timeout=10)
        self.tuner.close()
        self.wait("Civilization V closing", lambda: not game_processes(), timeout)

    def clean_up(self, copies):
        """Close the game and put the player's files back; returns what could not be. Stopped part way, as by a second
        Ctrl+C or an error it did not expect, it still puts the files back and says what is left before stopping."""
        problems = []
        try:
            if not self.handed_over:
                self.quit()
        except ERRORS as e:
            problems.append("%s; close Civilization V by hand" % e)
        except BaseException as e:
            # After Ctrl+C the player wants it to stop now, so the game is left for them to close.
            if isinstance(e, Exception):
                with contextlib.suppress(Exception):
                    self.quit(30)
            self.put_back_now(copies)
            raise
        problems = self.files.put_back(copies, game_running=bool(game_processes())) + problems
        if not problems:
            self.say("back as before this run: %s" % files.and_list([item.label for item in copies.items]))
        return problems

    def put_back_now(self, copies, autosaves_touched=True):
        """For a run stopping at once: put back what the game, as it is now, lets go of, and say what is left."""
        running = bool(game_processes())
        for problem in self.files.put_back(copies, autosaves_touched, running):
            self.say(problem)
        if running and self.started is not None:
            self.say("close Civilization V by hand")

    def wait_until_closed(self):
        """Hand the game to the player until they close it, however long they play. The tuner port is let go, so
        FireTuner can use it meanwhile. A first Ctrl+C only says how to finish; a second leaves the game open."""
        self.handed_over = True
        self.tuner.close()
        self._left_to_player("Close Civilization V to finish, and your files go back then.")

    def hand_over(self, message):
        self.files.begin_hand_over()
        try:
            # Keys typed before the hand-over would otherwise hand the game back the moment it starts.
            enter_pressed()
            self.say(message)
            self.say(hand_back_how())
            self.handed_over = True
            # FireTuner can connect meanwhile, and what Lua prints does not pile up unread.
            self.tuner.close()
            back = self._left_to_player(
                "Hand the game back to finish the run.", lambda: enter_pressed() or self.files.handed_back()
            )
        finally:
            self.files.end_hand_over()
        if not back:
            raise GameClosed(CLOSED_DURING_HAND_OVER)
        self.handed_over = False
        try:
            self.tuner.connect(10)
        except tuner.TunerError:
            if not game_processes():
                raise GameClosed(CLOSED_DURING_HAND_OVER)
            self.tuner.connect(60)
        self.say("took the game back")

    def _left_to_player(self, finish_how, handed_back=lambda: False):
        warned, closed_polls, look_at = False, 0, 0
        # One empty answer from tasklist can be a glitch, and taking it for a closed game ends the player's session.
        while closed_polls < 2:
            try:
                if handed_back():
                    return True
                if time.time() >= look_at:
                    look_at = time.time() + LOOK_EVERY
                    self.files.beat()
                    closed_polls = 0 if game_processes() else closed_polls + 1
                time.sleep(0.5)
            except KeyboardInterrupt:
                if warned:
                    raise
                warned = True
                self.say("%s %s" % (finish_how, LEFT_OPEN))
        return False

    def log(self, name="Bazaar.log"):
        return self.files.log(name, self.started)


@contextlib.contextmanager
def session(game, seed=None, staged=(), saves=(), windowed=False):
    """Install the staged mods into MODS, start the game, with these seeds unless seed is None and in a window if
    windowed, and yield the folders the game loads the mods from. Afterwards close the game and put back everything the
    run changed: MODS, the Mods menu's choices, the plugin order, config.ini, the display settings, the autosaves and
    the saves of these names. A problem putting things back is reported without hiding the error that ended the run.
    Preflight must have found the game closed, since installing replaces folders a running game holds."""
    copies = game.files.copy_aside(saves, game.files.changed_by(staged), windowed)
    try:
        expected = game.files.install(staged)
        if seed is not None:
            game.files.use_seed(seed)
        if windowed:
            game.files.use_windowed()
        game.start()
    except BaseException:
        game.put_back_now(copies, autosaves_touched=False)
        raise
    try:
        yield expected
    except BaseException:
        for problem in game.clean_up(copies):
            game.say(problem)
        raise
    problems = game.clean_up(copies)
    if problems:
        raise CheckError("; ".join(problems))
