"""Run a checks file: start Civilization V with the mods it names and the mods they depend on, play its steps, check
what happened, and put back everything the run changed in the player's folder. With --play, switch the mods on and
leave the game to the player instead; closing it puts everything back. --hand-back gives the game back to a run waiting
at a hand_over step.

    python -m bazaar_check checks.toml
    python -m bazaar_check --play checks.toml
    python -m bazaar_check --hand-back
"""

import collections
import contextlib
import os
import re
import tempfile
import tomllib

from . import CheckError, files, game, tuner

SETTINGS = {
    "mods": (list, tuple),
    "plugin_order": (list, tuple),
    "steps": (list, tuple),
    "crash_expected": (bool, list, tuple),
    "lua_errors_expected": (bool,),
    "seed": (int,),
}
TYPE_NAMES = {str: "text", int: "a whole number", bool: "true or false", list: "a list", tuple: "a list"}
# The game reads its seeds as signed 32-bit numbers.
MAX_SEED = 2**31 - 1
Step = collections.namedtuple("Step", "value needs may")
STEPS = {
    "start_game": Step(str, {}, {}),
    "end_turns": Step(int, {}, {}),
    "save": Step(str, {}, {}),
    "load": Step(str, {}, {}),
    "main_menu": Step(bool, {}, {}),
    "lua": Step(str, {}, {"state": str, "has": str, "within": int, "what": str}),
    "run": Step(str, {}, {"state": str}),
    "log": Step(str, {}, {"has": str, "lacks": str, "count": int, "since": str, "within": int, "what": str}),
    "mark": Step(str, {}, {}),
    "plugin": Step(str, {}, {"status": str, "problems": str, "what": str}),
    "description_of": Step(str, {"version": int, "is": str}, {"what": str}),
    "hand_over": Step(str, {}, {}),
}
PATTERNS = ("has", "lacks", "problems")
STATUSES = ("Loaded", "Not loaded", "Turned off after a crash", "Skipped")
CRASH = r"^Plugin (?P<plugin>.+): (Faulted \(code |It stopped working)"
LUA_ERROR = r"(Runtime|Syntax) Error:"

# Any fixed seed makes every run of a checks file play the same game; 0 has the game take one from the clock.
DEFAULT_SEED = 1
Checks = collections.namedtuple(
    "Checks",
    "mods plugin_order steps crash_expected lua_errors_expected seed",
    defaults=((), (), (), False, False, DEFAULT_SEED),
)
Result = collections.namedtuple("Result", "what passed found")


def kind_of(step):
    return next(key for key in step if key in STEPS)


def check_step(number, step):
    kinds = [key for key in step if key in STEPS] if isinstance(step, dict) else []
    if len(kinds) != 1:
        raise CheckError("step %d must do one of %s" % (number, ", ".join(sorted(STEPS))))
    kind = kinds[0]
    spec = STEPS[kind]
    missing = set(spec.needs) - set(step)
    if missing:
        raise CheckError("step %d (%s) needs %s" % (number, kind, ", ".join(sorted(missing))))
    types = dict(spec.needs, **spec.may, **{kind: spec.value})
    for key, value in step.items():
        if key not in types:
            raise CheckError("step %d (%s) does not take %s" % (number, kind, key))
        if type(value) is not types[key]:
            raise CheckError("step %d (%s): %s needs %s" % (number, kind, key, TYPE_NAMES[types[key]]))
    if kind == "log" and len({"has", "lacks"} & set(step)) != 1:
        raise CheckError("step %d (log) needs has or lacks, not both" % number)
    if kind == "log" and "within" in step and "lacks" in step:
        raise CheckError("step %d (log): within waits for a line to appear, so it does not go with lacks" % number)
    if "count" in step and ("has" not in step or step["count"] < 1):
        raise CheckError("step %d (log) takes count only with has, and needs 1 or more" % number)
    if kind == "plugin" and step.get("status", STATUSES[0]) not in STATUSES:
        raise CheckError(
            "step %d (plugin): status is one of %s" % (number, ", ".join('"%s"' % status for status in STATUSES))
        )
    if kind in ("save", "load") and (re.search(r'[\\/:*?"<>|]', step[kind]) or not step[kind].strip(" .")):
        raise CheckError('step %d (%s) needs a name that can be a file name: no \\ / : * ? " < > |' % (number, kind))
    if kind == "start_game" and step[kind] != "duel":
        raise CheckError('step %d (start_game) can only start a "duel" game' % number)
    if kind == "end_turns" and step[kind] < 1:
        raise CheckError("step %d (end_turns) needs 1 or more" % number)
    if kind == "main_menu" and not step[kind]:
        raise CheckError("step %d (main_menu) needs true" % number)
    if kind == "hand_over" and not step[kind].strip():
        raise CheckError("step %d (hand_over) needs text saying what to do with the game" % number)
    if step.get("within", 1) < 1:
        raise CheckError("step %d (%s): within needs 1 or more seconds" % (number, kind))
    for key in set(PATTERNS) & set(step):
        try:
            re.compile(step[key])
        except re.error as e:
            raise CheckError("step %d (%s) has a %s pattern Python cannot read: %s" % (number, kind, key, e))


def kinds_text(kinds):
    return " or ".join(dict.fromkeys(TYPE_NAMES[kind] for kind in kinds))


def validate(checks):
    """Refuse checks the game would only trip over part way through."""
    for name, kinds in SETTINGS.items():
        if type(getattr(checks, name)) not in kinds:
            raise CheckError("%s needs %s" % (name, kinds_text(kinds)))
    if not checks.mods:
        raise CheckError("the checks name no mods")
    if not all(isinstance(mod, str) for mod in checks.mods):
        raise CheckError("mods needs a list of folders and packages")
    if not 0 <= checks.seed <= MAX_SEED:
        raise CheckError("seed needs a whole number from 0 to %d" % MAX_SEED)
    if type(checks.crash_expected) is not bool and not all(isinstance(name, str) for name in checks.crash_expected):
        raise CheckError(
            'crash_expected needs true, false, or a list of plugin names as Bazaar.log gives them, such as "My Plugin"'
        )
    for number, entry in enumerate(checks.plugin_order, 1):
        problem = files.order_entry_problem(entry)
        if problem:
            raise CheckError("plugin_order entry %d: %s" % (number, problem))
    marks = set()
    for number, step in enumerate(checks.steps, 1):
        check_step(number, step)
        if "since" in step and step["since"] not in marks:
            raise CheckError('step %d (log): no step before it marks "%s"' % (number, step["since"]))
        if kind_of(step) == "mark":
            marks.add(step["mark"])


def read_checks(path):
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise CheckError("%s could not be read: %s" % (path, e))
    unknown = sorted(set(data) - set(SETTINGS))
    if unknown:
        raise CheckError("%s has a setting the checks do not know: %s" % (path, ", ".join(unknown)))
    checks = Checks(**data)
    try:
        validate(checks)
    except CheckError as e:
        raise CheckError("%s: %s" % (path, e))
    folder = os.path.dirname(os.path.abspath(path))
    return checks._replace(mods=[os.path.normpath(os.path.join(folder, mod)) for mod in checks.mods])


def versions_text(low, high):
    if (low, high) == (0, files.ANY_VERSION):
        return "any version"
    return "version %d" % low if low == high else "version %d to %d" % (low, high)


def checked_versions(expected):
    wanted = {}
    for folder in expected:
        info = files.read_modinfo(folder)
        if info.id in wanted:
            raise CheckError(
                "mods lists two versions of %s (%d and %d): keep one" % (info.name, wanted[info.id], info.version)
            )
        wanted[info.id] = info.version
    return wanted


def dependencies(expected, installed, mods_folder):
    """The (id, version) pairs to switch on: the checked mods, then, for each mod they need and each mod those need,
    the newest installed version that every need allows. A test build is used only when the checks file lists it."""
    wanted = checked_versions(expected)
    checked = set(wanted)
    allowed = {}
    needing = [files.read_modinfo(folder) for folder in expected]
    while needing:
        info = needing.pop()
        for need in info.dependencies:
            needs = "%s needs %s, %s" % (info.name, need.title, versions_text(need.low, need.high))
            if need.id in checked:
                if not need.low <= wanted[need.id] <= need.high:
                    raise CheckError("%s, but the checks file lists version %d" % (needs, wanted[need.id]))
                continue
            low, high = allowed.get(need.id, (need.low, need.high))
            low, high = max(low, need.low), min(high, need.high)
            if need.id in allowed and low <= wanted[need.id] <= high:
                allowed[need.id] = (low, high)
                continue
            versions = [v for m, v, _ in installed if m == need.id and low <= v <= high]
            usable = [v for v in versions if v < files.TEST_VERSION]
            if not usable and need.id in allowed:
                other = versions_text(*allowed[need.id])
                raise CheckError("%s, and another mod needs %s; no installed version suits both" % (needs, other))
            if not usable:
                test_build = (
                    " (only a version of %d or above is, which counts as a test build: list it in mods to use it)"
                )
                raise CheckError(
                    "%s, which is not installed%s" % (needs, test_build % files.TEST_VERSION if versions else "")
                )
            allowed[need.id] = (low, high)
            wanted[need.id] = max(usable)
            found = files.mod_folders(mods_folder, (need.id, wanted[need.id]))
            if found:
                needing.append(files.read_modinfo(found[0]))
    return list(wanted.items())


class Marks:
    """Where each log ended at each mark step, so a log check can look only at the lines written after it."""

    def __init__(self, steps):
        self.logs = {step["log"] for step in steps if "log" in step}
        self.ends = {}

    def record(self, g, name):
        self.ends[name] = {log: len(g.log(log)) for log in self.logs}

    def lines(self, g, log, since=None):
        lines = g.log(log)
        return lines[self.ends[since][log] :] if since else lines


def log_where(name, since):
    return "%s after %s" % (name, since) if since else name


def log_label(name, has=None, lacks=None, count=None, since=None):
    if lacks is not None:
        return "%s lacks %s" % (log_where(name, since), lacks)
    if count is None:
        return "%s has %s" % (log_where(name, since), has)
    return "%s has %s %d times" % (log_where(name, since), has, count)


def log_check(lines, name, has=None, lacks=None, what=None, count=None, since=None):
    what = what or log_label(name, has, lacks, count, since)
    if lacks is not None:
        found = [line for line in lines if re.search(lacks, line)]
        return Result(what, not found, "it has: " + found[0] if found else "")
    found = [line for line in lines if re.search(has, line)]
    if count is None:
        return Result(what, bool(found), "%s has no such line" % log_where(name, since))
    return Result(what, len(found) == count, "it has it %d times" % len(found))


def crash_check(g, expected):
    """No plugin crashed but those the checks expect to."""
    crashes = [line for line in g.log() if (m := re.match(CRASH, line)) and m.group("plugin") not in expected]
    what = "no plugin other than %s stopped working" % ", ".join(expected) if expected else "no plugin stopped working"
    return Result(what, not crashes, "Bazaar.log has: " + crashes[0] if crashes else "")


def asked(g, step, what, answer, passes):
    """Ask once, or with within, ask again until the answer passes or that many seconds run out. Returns the last
    answer and None, or None and why the game gave no answer, such as a Lua error. The game closing ends the run
    instead."""
    answers = []

    def ready():
        answers.append(answer())
        return passes(answers[-1])

    if "within" not in step:
        try:
            ready()
        except (tuner.NoSuchState, tuner.LuaError) as e:
            return None, str(e)
        return answers[-1], None
    try:
        g.wait(what, ready, step["within"])
    except game.GameClosed:
        raise
    except CheckError as e:
        if not answers:
            return None, str(e)
    return answers[-1], None


def lua_passes(step, value):
    return re.search(step["has"], value) is not None if "has" in step else value not in ("false", "nil")


def lua_check(g, step):
    state = step.get("state", game.IN_GAME)
    has = " has %s" % step["has"] if "has" in step else ""
    what = step.get("what") or "%s: %s%s" % (state, step["lua"], has)
    value, why = asked(g, step, what, lambda: g.text(state, step["lua"]), lambda value: lua_passes(step, value))
    if why:
        return Result(what, False, why)
    return Result(what, lua_passes(step, value), "it gave %s" % value)


def log_step(g, step, marks):
    options = {key: value for key, value in step.items() if key not in ("log", "within", "what")}
    what = step.get("what") or log_label(step["log"], **options)

    def result(lines):
        return log_check(lines, step["log"], what=what, **options)

    def lines():
        return marks.lines(g, step["log"], step.get("since"))

    found, why = asked(g, step, what, lines, lambda found: result(found).passed)
    return Result(what, False, why) if why else result(found)


def plugin_check(g, step):
    rows = {file.lower(): (status, problems) for file, status, problems in g.plugins()}
    status = " as %s" % step["status"] if "status" in step else ""
    problems = " with a problem matching %s" % step["problems"] if "problems" in step else ""
    shows = "shows %s%s%s" % (step["plugin"], status, problems) if status or problems else "lists %s" % step["plugin"]
    what = step.get("what") or "the Plugins screen " + shows
    row = rows.get(step["plugin"].lower())
    if row is None:
        return Result(what, False, "it does not list %s" % step["plugin"])
    status, problems = row
    passed = step.get("status", status) == status and re.search(step.get("problems", ""), problems) is not None
    return Result(what, passed, "it shows %s%s" % (status, ": " + problems if problems else ""))


def description_check(g, step):
    description = g.mod_description(step["description_of"], step["version"])
    what = step.get("what") or "the Mods menu describes %s v%d as expected" % (step["description_of"], step["version"])
    found = "it reads: ...%s" % description[-70:] if description else "the game gave no description"
    return Result(what, (description or "").strip() == step["is"].strip(), found)


def do_step(g, step, results, marks):
    kind = kind_of(step)
    if kind == "start_game":
        g.start_game()
    elif kind == "end_turns":
        for _ in range(step[kind]):
            g.end_turn()
    elif kind == "save":
        g.save(step[kind])
    elif kind == "load":
        g.load(step[kind])
    elif kind == "main_menu":
        g.to_main_menu()
    elif kind == "run":
        g.lua(step.get("state", game.IN_GAME), step[kind])
    elif kind == "lua":
        results.append(lua_check(g, step))
    elif kind == "log":
        results.append(log_step(g, step, marks))
    elif kind == "mark":
        marks.record(g, step[kind])
    elif kind == "plugin":
        results.append(plugin_check(g, step))
    elif kind == "description_of":
        results.append(description_check(g, step))
    elif kind == "hand_over":
        g.hand_over(step[kind])
    else:
        raise AssertionError("STEPS has %s, which do_step does not do" % kind)


def switch_on(checks, g, expected):
    """Switch on the checked mods and the mods they need, and no others, which loads Bazaar and its plugins."""
    keys = [files.mod_key(folder) for folder in expected]

    def listed():
        return set(keys) <= {(m, v) for m, v, _ in g.mods(enabled_only=False)}

    g.wait("the game listing the mods", listed, 120)
    problems = g.files.installed_problems(expected)
    if problems:
        raise CheckError("; ".join(problems))
    g.files.use_plugin_order(checks.plugin_order)
    installed = g.mods(enabled_only=False)
    wanted = dependencies(expected, installed, g.files.path("MODS"))
    names = {(m, v): name for m, v, name in installed}
    g.say("choosing " + ", ".join("%s (v %d)" % (names[key], key[1]) for key in wanted))
    g.set_mods(wanted)
    g.activate()


def play_steps(checks, g, expected, results):
    """The part of a run inside the game; it adds a Result to results as each check is made."""
    switch_on(checks, g, expected)
    marks = Marks(checks.steps)
    for number, step in enumerate(checks.steps, 1):
        try:
            do_step(g, step, results, marks)
        except game.GameClosed as e:
            if kind_of(step) != "hand_over":
                raise
            if number == len(checks.steps):
                return
            raise game.GameClosed("%s, so the steps after it did not run" % e)


def final_checks(checks, g):
    """Read once the game has closed, so a plugin that faults while the game shuts down counts too."""
    results = []
    if checks.crash_expected is not True:
        results.append(crash_check(g, checks.crash_expected or []))
    if not checks.lua_errors_expected:
        results.append(log_check(g.log("Lua.log"), "Lua.log", lacks=LUA_ERROR, what="Lua.log has no Lua errors"))
    return results


@contextlib.contextmanager
def checked_mods_in_game(checks, g, seed=None, saves=(), windowed=False):
    """With no other run going and the game closed, put back what a stopped run left, then start the game with the
    checked mods installed, and yield the folders it loads them from; everything goes back afterwards."""
    with files.one_run_at_a_time():
        g.preflight()
        g.files.put_back_leftovers()
        staging = tempfile.mkdtemp(prefix="Bazaar check mods ")
        try:
            staged = g.files.stage_all(checks.mods, staging)
            with game.session(g, seed, staged, saves, windowed) as expected:
                yield expected
        finally:
            with contextlib.suppress(OSError):
                files.remove_tree(staging)


def run_checks(checks, g, results):
    saves = [step["save"] for step in checks.steps if "save" in step]
    with checked_mods_in_game(checks, g, checks.seed, saves, windowed=True) as expected:
        play_steps(checks, g, expected, results)
    results.extend(final_checks(checks, g))


def play_by_hand(checks, g):
    """Start the game with the checked mods switched on, and leave it to the player until they close it. Returns
    None, or why it stopped early in words that follow "Playing ", such as "stopped: ..."."""
    try:
        with checked_mods_in_game(checks, g) as expected:
            switch_on(checks, g, expected)
            g.say(
                "Civilization V is yours: play, then close it, and your files go back as they were; saves you make"
                " by hand stay. Leave this window open until then."
            )
            g.wait_until_closed()
    except KeyboardInterrupt:
        return stopped_by_ctrl_c(g)
    except game.ERRORS + (OSError,) as e:
        return "stopped: %s" % e
    return None


def stopped_by_ctrl_c(g):
    if g.handed_over:
        return "stopped with the game still open: the next run of check.cmd or play.cmd puts your files back"
    return "stopped"


def print_results(results):
    for result in results:
        print("  %s  %s" % ("PASS" if result.passed else "FAIL", result.what), flush=True)
        if not result.passed and result.found:
            print("        " + result.found, flush=True)


def log_tail(g):
    """The end of Bazaar.log to follow a failure, or nothing if the game never started."""
    if g.started is None:
        return ""
    lines = g.log()[-15:]
    return "\nThe end of Bazaar.log:\n" + "\n".join(lines) if lines else "\nBazaar.log was not written in this run."


def run(checks, g):
    """Run the checks and print what each found; returns None, or why they did not all pass in words that follow
    "The checks ", such as "failed."."""
    try:
        validate(checks)
    except CheckError as e:
        return "could not start: %s" % e
    results = []
    try:
        run_checks(checks, g, results)
    except game.ERRORS + (OSError,) as e:
        if g.mods_on:
            with contextlib.suppress(OSError):
                results.extend(final_checks(checks, g))
        print_results(results)
        return "could not finish: %s%s" % (e, log_tail(g))
    print_results(results)
    if all(result.passed for result in results):
        return None
    return "failed." + log_tail(g)


def main(argv):
    if argv == ["--hand-back"]:
        if files.hand_back():
            print("handed the game back to the check")
            return 0
        print("no check is waiting for its game back")
        return 1
    playing = argv[:1] == ["--play"]
    paths = argv[1:] if playing else argv
    if len(paths) != 1:
        print(__doc__.strip())
        return 2
    try:
        checks = read_checks(paths[0])
        g = game.Game()
    except game.ERRORS as e:
        print(e)
        return 1
    if playing:
        stopped = play_by_hand(checks, g)
        if stopped:
            print("Playing " + stopped)
        return 1 if stopped else 0
    try:
        failed = run(checks, g)
    except KeyboardInterrupt:
        failed = stopped_by_ctrl_c(g)
    print("The checks " + (failed or "passed."))
    return 1 if failed else 0
