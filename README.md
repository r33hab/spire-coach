# Spire Coach

An offline coach for Slay the Spire 2. While you play, it reads the game through the
STS2_MCP mod, analyzes every combat decision in a local simulator of the game, and
shows the best move in a small panel that floats above the game. It also grades each
move you make, from Best to Blunder.

Everything runs on this Mac. The engine is a tree search in the simulator
(`~/Documents/git/sts2`), not a hosted model. The panel's server listens on 127.0.0.1
only, and nothing is sent anywhere.

Scope for now: single-player, the Ironclad, Act 1, game version v0.107.1. See
[Known limits](#known-limits).

## What you need

- macOS on Apple silicon, with Slay the Spire 2 v0.107.1 installed through Steam.
- uv, and Python 3.12.
- The .NET 9 SDK in `~/.dotnet`. `tools/env.sh` puts it and uv on your PATH: source it
  in each new terminal before the commands below.
- Three repositories:
  - `~/Documents/git/spire-coach`: this one.
  - `~/Documents/git/sts2`: the simulator, on its `spire-coach` branch, which includes
    the engine service (`sts2_gym.live.service`).
  - `~/Documents/git/STS2MCP`: the game mod, on its `spire-coach` branch.

Install the coach's own dependencies once:

    cd ~/Documents/git/spire-coach
    source tools/env.sh
    uv sync

From this folder, run each command below as `uv run spire-coach <command>`.

## Setup, in order

Close the game before steps 2 and 3. Those commands refuse to run while it is open.

1. **Build the simulator.** The coach starts the simulator's engine service by itself
   each time it runs, from `~/Documents/git/sts2`, so the simulator has to be built
   first:

       source ~/Documents/git/spire-coach/tools/env.sh
       cd ~/Documents/git/sts2
       uv sync --python 3.12
       bash scripts/build.sh osx-arm64

2. **`spire-coach mod install`** builds the mod from `~/Documents/git/STS2MCP` (or the
   checkout given with `--repo`) and installs it into the game's mods folder, together
   with the token the coach and the mod share. A previous install is backed up first.

3. **`spire-coach mod enable`** turns the mod on in the game's `settings.save`, after
   backing that file up.

4. **Launch the game** from Steam, or with `open "steam://rungameid/2868840"`. With the
   mod enabled the game runs modded, and a modded game has save profiles of its own.
   Pick your modded profile (see [Save profiles](#save-profiles)).

5. **`spire-coach doctor`**, with the game running, checks that everything is in place:
   the game folder and version, the mod files and token, the mod enabled in
   `settings.save`, the game running, and the mod answering with the right token and
   versions. Each check prints ✓, or ✗ with the reason. It exits 0 only when all pass.

6. **`spire-coach coach`** starts the coach: the engine service, the panel's server and
   the floating panel window. Then play as usual.

## Save profiles

While the mod is enabled, the game uses a separate set of modded profiles and leaves
your normal ones alone. Play with the coach on your modded profile. On this Mac that is
modded profile 2, which is fully unlocked.

Seeding a modded profile is optional. `spire-coach saves seed-modded --from 1 --to 1`
copies normal profile 1's progress and preferences into modded profile 1 (`--to 3`
seeds profile 3, the slot autopilot will use), after backing up the whole save folder to
`~/Documents/spire-coach-backups`. The game must be closed. Steam Cloud may override
copied saves with its own copy, though, so the game's own modded profiles are the safer
choice. `spire-coach saves restore <backup>` puts a backup back.

## Playing without the mod

The game runs modded for as long as the mod is enabled. To play unmodded, open the
game's Settings, go to its mods screen, untick STS2_MCP and restart the game. With no
mod loaded, the game is back on your normal profiles, and the coach has nothing to read.
To use the coach again, tick the mod in the same screen (or run `spire-coach mod enable`
with the game closed) and restart the game.

## Using the coach

    spire-coach coach [--workers N] [--budget S] [--port P] [--no-window] [--mode show|quiz]

| Option | Default | What it does |
|---|---|---|
| `--workers N` | 4 | Search worker processes the engine service runs. |
| `--budget S` | 1.5 | Seconds of search per position. |
| `--port P` | 15530 | The panel's port on 127.0.0.1. |
| `--no-window` | off | No floating window: the terminal prints the panel's URL; open it in a browser tab. |
| `--mode show\|quiz` | show | Show the best move at once, or quiz: hide it until you have moved. |

The coach starts the engine service and checks its health, then opens the panel in a
window that stays above the game. The terminal prints the panel's address,
`http://127.0.0.1:15530/`, and the same page works in any browser tab. If the window
cannot open, the coach says so and keeps serving the page for the browser.

The panel shows, from top to bottom:

- the act and floor, the Show/Quiz switch and the Compact/Expand button;
- a status line: waiting for the game, analyzing, advice ready, or why advice is off;
- the grade of your last move, with what was better and by about how much HP;
- the best move, and the rest of the turn the engine expects after it;
- the HP the fight is expected to cost you, and your risk of dying;
- warnings, such as content the simulator does not model or a recent fidelity mismatch;
- other moves, each with the extra HP it is expected to cost.

You never wait for the coach. Analysis runs in the background, and when you move before
it finishes, the coach starts on the new position. The unfinished analysis still grades
the move you made from it.

To stop, close the panel window or press Ctrl-C in the terminal. Either one stops
everything, the engine service included, and so does a plain `kill` or closing the
terminal. The coach turns a request to quit into closing its window, so that it can
stop in order. A macOS logout or restart asks it to quit too, so the first attempt
is cancelled once; the coach then closes, and a second attempt goes through.

### When advice is off

Advice needs the versions the engine was built for. The status line says what is wrong
when:

- the game is not v0.107.1;
- the mod is not the 0.4 build with payload schema 4;
- the engine service failed its health check, did not report the native API version
  it runs, or was built for another game version.

The **Show advice anyway** button under the status line (the version override) shows
advice regardless, with a warning that names the problem. **Turn off override** turns
it back off. Advice can also be off for a single position that the coach could not read
or the engine could not analyze; the status line then gives the reason.

When the engine is what keeps advice off, the coach checks it again every 30 s, and
whenever the engine service restarts. Advice comes on by itself once the engine is
healthy, without a restart of the coach. Each check waits up to 90 s for the service
to start, since a cold start (uv, imports, the search workers) can be slow;
`SPIRE_COACH_ENGINE_START_S` changes that.

## What the grades mean

After each move you make in combat, the coach compares it with the best move, using the
analysis of the position you moved from. A fight's value is your HP at its end, with each
potion still held worth 10 HP and a death counted as −100. The engine plays both moves
out many times on the same random outcomes, so the difference in value comes with an
error bar. The grade uses the low end of that difference's 90% confidence range
(difference − 1.645 × standard error), so a few unlucky samples never produce a harsh
grade. The death-risk rule for a Blunder uses the same kind of low end: the increase in
the risk of dying, minus 1.645 × its standard error.

| Grade | HP given up (the low end) |
|---|---|
| Best | 0.5 HP or less, or the engine's own best move |
| Good | under 2 HP |
| Inaccuracy | 2 to 5 HP |
| Mistake | 5 to 10 HP |
| Blunder | 10 HP or more, or when the low end of the increase in the risk of dying is 10 percentage points or more |

- **Ungraded**: the coach could not grade the move. The engine had fewer than 20 paired
  samples for it or lacked one of the statistics a grade needs, or there was no
  analysis of the position you moved from (you moved before that position settled on
  screen, or its analysis failed).
- **(unreliable)** after a grade: the position had content the simulator does not model
  fully. Take the grade as a hint.

The thresholds are in `coach/grader.py` (`Thresholds`).

## Known limits

- **Act 1, the Ironclad, game version v0.107.1.** The coach is built and checked for
  that. Elsewhere, expect more unreliable positions and weaker advice. A game update
  turns advice off until the simulator and the mod catch up.
- **Advice only when the position converts reliably.** Each position is converted into
  the simulator's terms. When the conversion is not reliable (a card, relic, power or
  enemy the simulator does not fully model), the panel still shows the engine's pick
  but marks it unreliable and gives the reason in its warnings, and grades from that
  position carry "(unreliable)". Only a position that converts reliably gets advice you
  can trust.
- **Combat only.** Card rewards, the map, shops, rests and events get no advice yet.
- **Single-player only.**
- Potions count a fixed 10 HP each, so the engine can be short-sighted about saving a
  potion or setting up for later turns.

## Files the coach writes

Under `data/` in this folder, which git ignores:

- `data/runs/<time>.jsonl`: the run log of each coach session: every settled state, your
  moves, the analyses, the grades and the fidelity checks.
- `data/gaps/<time>.json`: a fidelity mismatch, written when the simulator predicted a
  different outcome for your move than the game showed.
- `data/logs/engine.log`: the engine service's log.
- `data/captures/`: states recorded by `spire-coach capture`.

`SPIRE_COACH_DATA_DIR` moves the runs, gaps and captures elsewhere. The engine log
stays here.

## Other commands and settings

- `spire-coach capture` records every settled state and action to `data/captures/`,
  without the engine (for building test fixtures).
- `spire-coach saves backup` backs up the game's whole save folder to
  `~/Documents/spire-coach-backups`, and `spire-coach saves restore <backup>` puts one
  back. Both need the game closed.

Settings come from the environment: `SPIRE_COACH_BASE_URL` (the mod's address, default
`http://127.0.0.1:15526`), `SPIRE_COACH_TOKEN` (by default read from the mod's
`STS2_MCP.conf`), `SPIRE_COACH_POLL_S` (how often the game is read, default 0.2 s),
`SPIRE_COACH_ENGINE_START_S` (how long a health check waits for the engine service
to start, default 90 s), `SPIRE_COACH_DATA_DIR` and `SPIRE_COACH_MODS_DIR`.

## Development

    uv run pytest
    uv run ruff check .

The tests use fakes: a fake mod, a fake engine service and a stand-in window. They never
touch the game or the real simulator.
