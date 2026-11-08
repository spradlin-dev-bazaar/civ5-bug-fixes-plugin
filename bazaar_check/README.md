# Checking a mod in Civilization V

`check.cmd` builds your mod, then plays `checks.toml` in Civilization V with nobody at the keyboard: it starts the game, switches your mod on, plays the steps the file lists, and prints a line for each check, PASS or FAIL. Afterwards it closes the game and puts back everything it changed in your Civilization V folder. A run of a template's checks takes two or three minutes. Run it from a Command Prompt, so the results stay on screen when it ends.

`play.cmd` is for trying your mod by hand. It builds the mod, starts Civilization V with it and the mods it needs switched on, and leaves the game to you: set up a game and play, full screen if that is how you play. FireTuner can connect while you play. Leave its window open. When you close the game, your files go back as after a check: your own autosaves come back in place of the ones your game made, and `config.ini` and the Mods menu go back as they were. Saves you make yourself stay, so save by hand to keep a game. Ctrl+C in its window only says how to finish; a second Ctrl+C stops `play.cmd` and leaves the game open, and the next run of `check.cmd` or `play.cmd` puts your files back. You can start `play.cmd` by double-clicking it: if it fails, its window waits for a key, so you can read why.

The checker is the `bazaar_check` folder beside `check.cmd`. It is the same one Bazaar uses to check its own releases.

## Before the first run

- **Python 3.11 or newer**, from [python.org](https://www.python.org/downloads/windows/). `check.cmd` finds it through the `py` launcher the installer adds, or as `python` on the PATH.
- **The game's tuner port and logs switched on.** With Civilization V closed, open `Documents\My Games\Sid Meier's Civilization 5\config.ini` and set `EnableTuner = 1` and `LoggingEnabled = 1`. The checker talks to the game through the tuner port, the one FireTuner uses, and reads `Lua.log`, which the game writes only with logging on.
- **DirectX 11 chosen in Steam.** The checker starts the game through Steam, which starts the version you chose the last time you played. Start Civilization V from Steam once and choose DirectX 11.
- **Bazaar and Bazaar - VMC Bootstrap (required) installed**, from the Steam Workshop or Bazaar's GitHub release, if your mod needs Bazaar.

Close Civilization V before each run, and FireTuner too: the tuner port takes one program at a time. Runs go one at a time too: a run started while another is going stops at once.

## What a run does

1. `check.cmd` builds the mod, as `build.cmd` does.
2. The checker copies your Mods menu choices, plugin order, `config.ini`, `GraphicsSettingsDX11.ini`, autosaves, the saves of the names `checks.toml` saves under and the folders in MODS it is about to replace to a folder in Windows' temporary folder, and says where. That folder also gets a `where these go.txt` naming where each copy belongs.
3. It copies each mod `checks.toml` names into your MODS folder, replacing an older copy of the same mod. A mod folder that is already in MODS is checked where it is. If MODS holds another copy of the same mod and version under a different folder name, such as a package the game unpacked or a Workshop download, the run stops and names it: move it out of MODS.
4. It writes the run's seed into `config.ini`, sets `FullScreen = 0` in `GraphicsSettingsDX11.ini`, since a full-screen game stops answering once another window is in front of it, and starts Civilization V through Steam.
5. It switches on the mods `checks.toml` names, and the mods they need, and no others. For a mod they need, it takes the newest installed version their dependencies allow, leaving out test builds (mod version 900 or above) unless `checks.toml` names one.
6. It plays the steps.
7. It closes the game and puts back everything it copied, even when a check failed or the game crashed. The mods it added to MODS go, the folders it replaced come back, and the saves it made go.
8. It checks that no plugin stopped working, even as the game closed, and that `Lua.log` has no Lua errors.

It does not put back the game's logs, which the game empties each time it starts, or what mods store in `ModUserData` apart from Bazaar's plugin order.

Unless `checks.toml` sets `seed = 0`, every run starts the game with the same seed, and every `start_game` step sets up the same game: America against Rome, Prince difficulty, standard speed, ancient era, on a duel-sized Continents map. Other game options come from the game's own setup screen, as you last left them. So until your mod, Bazaar or those options change, every run of a checks file on your computer plays the same game, and the AI makes the same moves on the same turns.

## checks.toml

`checks.toml` is a [TOML](https://toml.io) file. Paths in it are relative to the file. Its settings:

| Setting | What it is |
|---|---|
| `mods` | The mods to check: mod folders, such as `"Build/My Mod"`, or `.civ5mod` packages. Required. |
| `plugin_order` | Optional. The order Bazaar loads plugins in, as on its Plugins screen: DLL names, such as `"MyPlugin.dll"`, or tables, such as `{ file = "MyPlugin.dll", skip = true }` or `{ file = "MyPlugin.dll", use_both_with = ["Other.dll"] }`. Without it, Bazaar uses its own order. |
| `steps` | What to do, in order: see below. |
| `crash_expected` | Optional. `true` when a plugin is meant to crash, or a list of the plugins that may, by the name they give Bazaar, such as `["My Plugin"]`. |
| `lua_errors_expected` | Optional. `true` when a Lua error is part of the check. |
| `seed` | Optional. The game's random seed, a whole number from 0 to 2147483647. Without it, the seed is 1; `0` lets the game take one from the clock, as it normally does. |

Each step is a table with one action:

| Step | What it does |
|---|---|
| `start_game = "duel"` | Starts a new game on a duel-sized Continents map, the only size it starts. |
| `end_turns = 3` | Ends that many turns. Before each, it spends every unit's moves and picks a unit to train in idle cities and something to research. A pop-up that holds up the turn is closed. |
| `save = "name"` | Saves the game under that name, in `ModdedSaves\single`. After the run the save goes, and a save of the same name you had comes back. |
| `load = "name"` | Loads that save. |
| `main_menu = true` | Goes back to the main menu. |
| `run = "Lua code"` | Runs Lua in the game. `state = "MainMenu"` runs it in another Lua state; the default is `InGame`. |
| `lua = "Lua expression"` | A check: passes unless the expression gives `false` or `nil`. `has = "pattern"` passes when its value matches the pattern instead. `within = 30` asks again for up to 30 seconds until it passes. A Lua error fails the check. `state` as for `run`. |
| `log = "Bazaar.log"` | A check on a log in `Documents\My Games\Sid Meier's Civilization 5\Logs`: `has = "pattern"` passes when a line matches, `lacks = "pattern"` when none does; a step takes one of the two. `count = 2` makes `has` need exactly that many lines; `since = "a mark"` looks only at lines written after that `mark` step; `within = 30` reads the log again for up to 30 seconds until `has` passes. |
| `mark = "a name"` | Remembers where every log ends, for a later `since`. |
| `plugin = "MyPlugin.dll"` | A check of the row Bazaar's Plugins screen shows for the DLL: `status = "Loaded"`, or `"Not loaded"`, `"Turned off after a crash"` or `"Skipped"`, word for word, and `problems = "pattern"`. Without either, it checks only that the screen lists the DLL. |
| `description_of = "mod ID"` | A check of a mod's description in the Mods menu: `version = 1` and `is = "the whole text"`. |
| `hand_over = "what to do"` | Leaves the game to you and shows the text: try the mod by hand, or with a program such as an AI connected over MCP. The checker waits as long as you take, and FireTuner can connect meanwhile. Hand the game back with Enter in the checker's window, or by running the `--hand-back` command it shows from any window, in the folder that holds `bazaar_check` (in PowerShell, put `&` and a space before it); the steps after it then run. Closing the game instead ends the run, and says so if steps were left. Saves you make yourself stay. Ctrl+C works as in `play.cmd`. |

A setting or a step the checker does not know, or a value it cannot use, stops it before the game starts.

Patterns are [Python regular expressions](https://docs.python.org/3/library/re.html#regular-expression-syntax): `^` matches the start of a log line, or of a Lua value or a Plugins screen problem, and `\.`, `\(` and `\[` match a dot, a bracket or a square bracket. In TOML, write patterns between single quotes, `'like this'`, so a backslash stays a backslash. Every check step also takes `what = "plain words"`, which the PASS or FAIL line shows in place of the pattern.

An example, for a plugin:

```toml
mods = ["Build/My Plugin"]

[[steps]]
plugin = "MyPlugin.dll"
status = "Loaded"

[[steps]]
start_game = "duel"

[[steps]]
end_turns = 2

[[steps]]
log = "Bazaar.log"
has = '^\[My Plugin\] doubled the score'
what = "My Plugin doubled the score"

[[steps]]
lua = "Players[Game.GetActivePlayer()]:GetScore() > 0"
```

## When a run fails

- **A check failed:** the FAIL line says what the check found, and the end of `Bazaar.log` follows. `Bazaar.log` and `Lua.log` keep everything the run wrote until the game next starts.
- **The run could not finish:** the message says why, such as a step `checks.toml` gets wrong, a mod the game would not switch on, or a turn that would not end. It names what the game was waiting for.
- **The game crashed or would not close:** the checker still puts your files back. While the game is still running, it leaves what the game holds, the Mods menu choices, `config.ini`, `GraphicsSettingsDX11.ini`, autosaves and MODS, and names the folder with the copies. Close the game: the next run, or `play.cmd`, puts them back first. To do it by hand instead, follow `where these go.txt` in that folder, then delete the folder.
- **A run was stopped before it put your files back,** for example by closing its window: the next run, or `play.cmd`, puts the copies back first, once the game is closed, and says so. A file that changed after the run stopped, such as an autosave of a game you played since, stays as it is, and the message names it. Your Mods menu choices go back all the same, undoing any change you made to them since, as do the lines the run set in `config.ini` and `GraphicsSettingsDX11.ini`.
