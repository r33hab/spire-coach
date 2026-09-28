"""The fight lab: an unattended, fully local program that plays many real-game fights
and records every move, so the simulator's fidelity checker (`spire-coach
fidelity-scoreboard`) gets thousands of real transitions without anyone playing.

No Claude, LLM or network while it runs: the mod's localhost API sets up each fight and
executes moves, the coach's own engine service chooses every move
(`coach.autoplay.MovePlayer`, the same `analyze`/`check` calls the live coach makes),
and the coach's normal session log records everything (`coach.runlog.RunLog`, under
`data/runs/fightlab-<timestamp>.jsonl`) so the scoreboard picks the session up with no
changes.

One fight at a time, each on its own fresh run: embark on modded profile 2 at the
given ascension, fast-forward to the encounter's act with `debug_enter_next_act` (so
the run's own `run.act` matches the encounter -- otherwise its checks would land in
the wrong act's bucket in the scoreboard), jump straight to the chosen encounter with
`debug_start_encounter`, stage a sampled deck, play it out, then return to the main
menu regardless of the result. A fresh run per fight means "the player died" and "the
fight is over, start the next one" are the same code path -- there is no separate
death recovery to get wrong.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from coach import autoplay, deckseed, navigator
from coach.autoplay import MovePlayer
from coach.bridge.client import BridgeError, ModClient
from coach.encounters import BOSS, ELITE, Encounter, FightPicker, rank_encounters
from coach.engine import EngineClient
from coach.runlog import RunLog

DEFAULT_TIME_SCALE = 3.0
DEFAULT_ACTS: tuple[int, ...] = (2, 3, 1)
_START_ENCOUNTER_TIMEOUT_S = 20.0


@dataclass
class FightRecord:
    encounter: str
    act: int
    tier: str
    won: bool
    moves: int


@dataclass
class LabSummary:
    fights_played: int = 0
    moves_recorded: int = 0
    wins: int = 0
    deaths: int = 0
    failures: int = 0  # could not be set up or got stuck: recovered, not counted as a death
    by_encounter: dict[str, dict[str, int]] = field(default_factory=dict)
    records: list[FightRecord] = field(default_factory=list)

    def add(self, record: FightRecord) -> None:
        self.records.append(record)
        self.fights_played += 1
        self.moves_recorded += record.moves
        bucket = self.by_encounter.setdefault(record.encounter, {"wins": 0, "deaths": 0})
        if record.won:
            self.wins += 1
            bucket["wins"] += 1
        else:
            self.deaths += 1
            bucket["deaths"] += 1

    def format(self) -> str:
        lines = [
            f"fight-lab: {self.fights_played} fight(s) played, {self.moves_recorded} "
            f"move(s) recorded -- {self.wins} win(s), {self.deaths} death(s)"
            + (f", {self.failures} setup failure(s)" if self.failures else "")
        ]
        for name in sorted(self.by_encounter):
            bucket = self.by_encounter[name]
            lines.append(f"  {name}: {bucket['wins']} win(s), {bucket['deaths']} death(s)")
        return "\n".join(lines)


def run(
    client: ModClient,
    engine: EngineClient,
    runlog: RunLog,
    *,
    fights: int,
    acts: Sequence[int] = DEFAULT_ACTS,
    ascension: int = 10,
    buffer_hp: int = 0,
    time_budget_s: float = 1.5,
    fidelity_doc: dict | None = None,
    shards_root: Path = deckseed.DEFAULT_SHARDS_ROOT,
    rng: random.Random | None = None,
    game_version: str = "unknown",
    stop: Callable[[], bool] = lambda: False,
    say: Callable[[str], None] = lambda _message: None,
    move_player: MovePlayer | None = None,
) -> LabSummary:
    """Play fights until `fights` is reached or `stop()` says otherwise. `move_player`
    is a seam for tests: a stand-in that skips real combat mechanics while still
    exercising the fight lab's own setup/recovery loop. `buffer_hp` (elites and bosses
    only) raises current and max HP at the start of the fight with `debug_gain_max_hp`
    -- HP only, never attack or Strength -- so a fight lasts long enough to show every
    move and phase instead of ending before the roster's later moves ever appear."""
    rng = rng or random.Random()
    ranked = rank_encounters(fidelity_doc, acts=acts)
    picker = FightPicker(ranked)
    player = move_player or MovePlayer(
        client, engine, runlog, time_budget_s=time_budget_s, game_version=game_version
    )
    runlog.append(
        "session",
        {
            "command": "fight-lab",
            "fights": fights,
            "acts": list(acts),
            "ascension": ascension,
            "buffer_hp": buffer_hp,
            **navigator.session_header(client.get_state()),
        },
    )
    summary = LabSummary()
    try:
        while summary.fights_played + summary.failures < fights and not stop():
            encounter = picker.next()
            say(
                f"fight {summary.fights_played + summary.failures + 1}/{fights}: "
                f"{encounter.name} (act {encounter.act}, {encounter.tier})"
            )
            moves_before = player.stats.moves
            try:
                record = _play_one(
                    client,
                    player,
                    encounter,
                    ascension=ascension,
                    buffer_hp=buffer_hp,
                    rng=rng,
                    shards_root=shards_root,
                    runlog=runlog,
                )
            except (BridgeError, navigator.NavigatorError) as exc:
                say(f"  could not play this fight ({exc}); recovering")
                runlog.append(
                    "error", {"error": f"fightlab: {exc}", "encounter": encounter.name}
                )
                summary.failures += 1
                picker.record(encounter, player.stats.moves - moves_before)
                _recover(client)
                continue
            summary.add(record)
            picker.record(encounter, record.moves)
            say(f"  -> {'won' if record.won else 'died'}, {record.moves} move(s)")
    except KeyboardInterrupt:
        say("fight-lab: stopping (Ctrl-C)")
    return summary


def _play_one(
    client: ModClient,
    player: MovePlayer,
    encounter: Encounter,
    *,
    ascension: int,
    buffer_hp: int,
    rng: random.Random,
    shards_root: Path,
    runlog: RunLog,
) -> FightRecord:
    navigator.embark_run(client, ascension=ascension)
    if encounter.act > 1:
        navigator.enter_act(client, encounter.act)
    navigator.leave_the_map(client)  # debug_start_encounter needs a real room, not the map itself
    client.post_action({"action": "debug_start_encounter", "encounter": encounter.name})
    state = navigator.poll_until(
        client,
        lambda s: s.get("state_type") == encounter.tier,
        timeout_s=_START_ENCOUNTER_TIMEOUT_S,
    )
    if state.get("state_type") != encounter.tier:
        raise navigator.NavigatorError(
            f"debug_start_encounter({encounter.name}) never reached the fight "
            f"(state_type={state.get('state_type')!r})"
        )
    if buffer_hp > 0 and encounter.tier in (ELITE, BOSS):
        # HP only, on purpose (spec): debug_gain_max_hp raises current and max HP and
        # rolls nothing else, so a longer fight is not also an easier one -- attack or
        # Strength would shorten it and hide the boss's later moves, the opposite of
        # the point. Tops up to buffer_hp rather than adding it every fight (bug 2/7:
        # unconditional adding reached 110,080 max HP over repeated fights).
        navigator.apply_hp_buffer(
            client,
            runlog,
            buffer_hp,
            navigator.max_hp_of(state),
            source="fightlab",
            log_extra={"encounter": encounter.name},
        )
    cards = deckseed.sample_deck(encounter.act, shards_root=shards_root, rng=rng)
    staged: list[str] = []
    if cards:
        staged = deckseed.stage_deck(client, cards)
    try:
        client.post_action({"action": "debug_force_play_phase"})
    except BridgeError:
        pass
    state = autoplay.wait_settled(client)
    if staged:
        # Spec: "verify it from the game state after setup" -- debug_add_card answers
        # "ok" whether or not the card actually reached a pile this fight can draw from
        # (fire-and-forget, and the wrong pile name once passed every check silently).
        missing = deckseed.verify_staged(state, staged)
        if missing:
            runlog.append(
                "error",
                {
                    "error": (
                        f"fightlab: {len(missing)}/{len(staged)} sampled card(s) missing "
                        f"from the deck after setup: {sorted(set(missing))}"
                    ),
                    "encounter": encounter.name,
                },
            )
    moves_before = player.stats.moves
    outcome = player.play_fight(state)
    moves = player.stats.moves - moves_before
    if not outcome.ok:
        # play_fight gave up (a refused move, an unreadable snapshot, the max_moves
        # safety cap): the fight is not actually over, so it is neither a win nor a
        # death -- counted as a setup-style failure instead of guessing either way.
        navigator.leave_run(client, outcome.state)
        raise navigator.NavigatorError(
            f"play_fight against {encounter.name} did not finish ({moves} move(s) made)"
        )
    won = outcome.state.get("state_type") != "game_over" or (outcome.last_hp or 0) > 0
    navigator.leave_run(client, outcome.state)
    return FightRecord(
        encounter=encounter.name, act=encounter.act, tier=encounter.tier, won=won, moves=moves
    )


def _recover(client: ModClient) -> None:
    """Best-effort return to the main menu after a fight fell over, so the next one
    starts from a known place instead of compounding the failure."""
    try:
        state = client.get_state()
    except BridgeError:
        time.sleep(2.0)
        return
    try:
        navigator.leave_run(client, state)
    except BridgeError:
        pass
