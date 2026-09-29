"""Play one fight with the coach's own engine, and record it in the coach's normal
session format so the fidelity scoreboard picks it up with no changes: `state`,
`analysis`, `action` and `check` entries, exactly the kinds `coach.live.CoachSession`
writes for a human -- just chosen by the engine and executed through the mod's API
instead of graded from a click.

Shared by `fight-lab` and `autopilot` (neither reimplements the other's move player or
recording): both call `MovePlayer.play_fight` in a loop from a settled combat state
and stop when it returns.

Why this is its own loop rather than reusing `CoachSession`: that session only
fidelity-checks a move whose action-log entry is tagged `source: "human"` (Review
Focus 3 there -- an API-made move is never graded, so it is never checked either). A
move fight-lab and autopilot make through the API is always tagged `"api"`, so running
them through `CoachSession` unmodified would record `state` and `action` but silently
skip every `check` -- exactly the records the fidelity scoreboard counts. This module
checks every move regardless of who made it, since here every move IS the engine's own
choice; there is nothing to grade, only to verify.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass

from coach.bridge.client import BridgeError, ModClient
from coach.bridge.decisions import (
    COMBAT_STATE_TYPES,
    Decision,
    DecisionKind,
    classify,
    fingerprint,
    is_settled,
)
from coach.engine import EngineClient, EngineError, Move, MoveStat
from coach.runlog import RunLog
from coach.snapshot import SnapshotError, build_live_snapshot

DEFAULT_SETTLE_TIMEOUT_S = 20.0
DEFAULT_POLL_S = 0.3
DEFAULT_MAX_MOVES = 300  # a stuck-safety cap, not a real turn limit ("a timeout, then recovery")
# No hopeless repeats (spec): a generous no-progress cap on consecutive turns without a
# new low in total alive-enemy hp, checked once per round. Calibrated against a real,
# eventually-won fight-lab capture (Decimillipede, fightlab-20260928-213922.jsonl,
# session buffer at 5000 hp): its total enemy hp went 4 full rounds at its longest
# without beating its own running low (segments reattaching), across 34 rounds overall
# -- slow, not stalled. 15 gives that more than 3x headroom while stopping well short
# of DEFAULT_MAX_MOVES on an actual wall (The Insatiable kills in 5 turns unless
# specific cards are played; Aeonglass/Entomancer flood the hand instead of dealing
# damage) rather than burning the full 300-move cap every single attempt.
DEFAULT_NO_PROGRESS_ROUNDS = 15
# The autopilot's own no-progress threshold (`no_progress_rounds`, when
# `auto_kill_no_progress` is on -- coach.autopilot.run's default): tighter than
# fight-lab's 15, because reaching it no longer means giving up on the fight -- it means
# calling debug_kill_enemy and moving on (a real wall like The Insatiable, Aeonglass,
# Entomancer, Lagavulin Matriarch's Strength drain to 0 damage, or the Queen's Bound;
# spec 2026-09-29). A whole unattended run should not spend 15 rounds confirming a wall
# it is about to clear itself anyway; 8 is still generous next to the Decimillipede's
# worst observed plateau (4 rounds, see above).
DEFAULT_AUTO_KILL_NO_PROGRESS_ROUNDS = 8
# How many consecutive end_turn sends with no round/turn change count as "ignored"
# (spec: a multi-stage boss's first stage dying left end_turn ignored 10 times in a row
# live, 2026-09-29, while a manual click worked) before autoplay tries to recover it.
END_TURN_STALL_THRESHOLD = 2
# Bug 3/7: confirmed live twice tonight (Ovicopter and, in this same session, a second
# fight) -- is_resolving stuck true, the play phase reported true, cards marked
# can_play, yet the game refused all input, even a manual click; only a full game
# restart cleared it. "About 60s" per spec: generous next to a real settle
# (DEFAULT_SETTLE_TIMEOUT_S=20s covers an ordinary slow turn already), short enough
# that the autopilot does not sit for minutes on something a human would recognize
# as dead within a few seconds of watching it.
DEFAULT_JAM_TIMEOUT_S = 60.0


class GameJamError(Exception):
    """`is_resolving` stayed true with no change in position for about
    `DEFAULT_JAM_TIMEOUT_S` seconds: the game has stopped responding to all input.
    Never recovered by waiting longer or retrying the same move (spec, and tonight's
    own experience) -- only a game restart is known to clear it. By default this stops
    the autopilot with a clear message (`coach.autopilot.run`); `--restart-on-jam`
    handles it instead."""


def in_combat(state: dict) -> bool:
    """True from `debug_start_encounter` until the fight truly ends (win or death):
    `state_type` stays a combat one through the main turn loop, and `in_combat` covers
    the in-combat selection screens, whose own `state_type` (hand_select, card_select)
    is not itself a combat type (coach.bridge.decisions._is_combat_selection)."""
    return state.get("state_type") in COMBAT_STATE_TYPES or bool(state.get("in_combat"))


def playable_choice(state: dict, analysis) -> Move:
    """The search's pick, unless the game itself won't let that card be played.

    An affliction the simulator does not model -- the Queen's Bound (live 2026-09-29) --
    marks a card `can_play: false`; the autopilot re-sent the search's pick forever while
    the game refused it. The game's own flag wins: take the most-visited move it allows,
    else end the turn.
    """
    hand = {c.get("index"): c for c in (state.get("player") or {}).get("hand") or []}

    def allowed(move) -> bool:
        return move.kind != "card" or hand.get(move.card_index, {}).get("can_play", True)

    if allowed(analysis.best):
        return analysis.best
    for move in sorted(analysis.moves, key=lambda m: m.visits, reverse=True):
        if allowed(move):
            return _plain_move(move)
    fallback = next((m for m in analysis.moves if m.kind == "end_turn"), analysis.best)
    return _plain_move(fallback)


def _plain_move(move):
    """A `MoveStat` as the plain `Move` it names: the logs and the fidelity replay
    expect only a move's identity fields, not its search statistics."""
    if isinstance(move, MoveStat):
        return Move(**{f.name: getattr(move, f.name) for f in dataclasses.fields(Move)})
    return move


def move_to_action(move: Move) -> dict | None:
    """One `/api/v1/singleplayer` action for a `coach.engine.Move`. None for a move
    kind the mod has no direct action for (there is none today; kept so a future move
    kind fails soft -- fight-lab logs it and recovers -- instead of crashing a run)."""
    if move.kind == "end_turn":
        return {"action": "end_turn"}
    if move.kind == "card":
        action = {"action": "play_card", "card_index": move.card_index}
    elif move.kind == "potion":
        action = {"action": "use_potion", "slot": move.potion_slot}
    elif move.kind == "select":
        return {"action": "combat_select_card", "card_index": move.select_index}
    else:
        return None
    if move.target:
        action["target"] = move.target
    return action


def _forced_selection_move(selection: dict) -> Move | None:
    """A deterministic stand-in for a mandatory in-combat selection the engine has no
    model for.

    Confirmed live (Knowledge Demon's forced curse pick, fightlab-20260928-213922):
    when `pending_selection`'s source is a power or unknown -- not a card, potion or
    relic -- the engine's own `analyze` has nothing to search and its `best` degrades
    to a no-op such as `end_turn`. This forces the first offered candidate instead, so
    the screen always resolves -- the same "first option" doctrine as the non-combat
    generic fallback (`coach.navigator.advance_one_step`), not good play, just
    termination. Only used when stopping (confirming or skipping) is not itself a
    legal move (see `_selection_step`); this is the last resort, not a shortcut around
    the min/max/can_skip rules that govern that.
    None when there is nothing to force a choice from (no candidates), so the caller
    falls back to the engine's own (still useless) move rather than crash."""
    candidates = selection.get("candidates") or []
    if not candidates:
        return None
    return Move(
        kind="select",
        card_index=None,
        card_uid=None,
        card_id=candidates[0].get("id"),
        potion_slot=None,
        potion_id=None,
        target=None,
        select_index=0,
        label="forced: first candidate (engine has no model for this selection's source)",
    )


def _select_action(kind: str, index: int) -> dict:
    """The mod action that presses candidate `index` on a pending in-combat
    selection. `kind` is `pending_selection.kind` (schema section 1.1): HAND is a pick
    from the player's own hand (Armaments' upgrade, Burning Pact's discard --
    `NPlayerHand.IsInCardSelection`, STS2MCP `ExecuteCombatSelectCard`); GRID and
    CHOOSE are both a different screen entirely (a card grid overlay -- Headbutt's
    discard-pile fetch -- or a "choose one of these" overlay a potion or card can open
    -- STS2MCP `NCardGridSelectionScreen`/`NChooseACardSelectionScreen`, both driven by
    `ExecuteSelectCard`, which `ExecuteCombatSelectCard` has no path to at all: it only
    ever looks at the hand). Sending `combat_select_card` for a GRID/CHOOSE screen is
    the bug that broke the very first fight tonight (autopilot-20260929-002615.jsonl:
    a Colorless Potion's CHOOSE screen looped 42 times, `selected` always `[]`, since
    the hand-only action never touched the overlay actually open)."""
    if kind == "HAND":
        return {"action": "combat_select_card", "card_index": index}
    return {"action": "select_card", "index": index}


def _confirm_action(kind: str) -> dict | None:
    """Finish a selection that has enough picks to stop on (`_selection_step` only
    calls this once that is true). HAND and GRID both need an explicit confirm
    (`combat_confirm_selection`/`confirm_selection`); CHOOSE does not and has none --
    a single `select_card` press already resolves it (STS2MCP `ExecuteConfirmSelection`
    explicitly errors "Choose-a-card screen requires no confirmation" for it), so this
    returns None and the caller sends nothing further."""
    if kind == "HAND":
        return {"action": "combat_confirm_selection"}
    if kind == "CHOOSE":
        return None
    return {"action": "confirm_selection"}


def _skip_action(kind: str) -> dict:
    """Decline a skippable selection with nothing picked yet. HAND has no separate
    skip control -- confirming with zero selected (only ever attempted when `min` is 0)
    is how a hand pick is declined. GRID and CHOOSE both skip through
    `cancel_selection` (STS2MCP `ExecuteCancelSelection`: the choose screen's own Skip
    button, or the grid's Close, whichever is open)."""
    if kind == "HAND":
        return {"action": "combat_confirm_selection"}
    return {"action": "cancel_selection"}


_NO_ENGINE_MOVE = Move(
    kind="none",
    card_index=None,
    card_uid=None,
    card_id=None,
    potion_slot=None,
    potion_id=None,
    target=None,
    select_index=None,
    label="(no engine decision)",
)


def _selection_step(selection: dict, move: Move) -> tuple[Move, dict | None]:
    """What to do about one pending in-combat selection (schema section 1.1), given
    the engine's proposed `move` for this decision. Never sends more than one action:
    the caller re-decides from scratch once the next position settles, so a multi-pick
    screen is a sequence of ordinary decisions, one pick per round trip, exactly like
    the engine's own `MoveStat.select_indices` models it -- not a loop this function
    runs itself.

    Rules (spec, bug 1&7/7): never pick past `max`; never confirm below `min`; skip
    (nothing picked) only when `can_skip` allows it; otherwise keep picking, forcing
    the first candidate only when the engine's own move cannot be trusted at all
    (no legal way to stop yet, and it did not offer a pick)."""
    kind = str(selection.get("kind") or "HAND")
    min_ = selection.get("min")
    min_ = min_ if isinstance(min_, int) else 0
    max_ = selection.get("max")
    max_ = max_ if isinstance(max_, int) else None
    selected_count = len(selection.get("selected_uids") or [])
    can_skip = bool(selection.get("can_skip"))
    at_max = max_ is not None and selected_count >= max_
    met_min = selected_count >= min_

    if at_max:
        return move, _confirm_action(kind)
    if move.kind == "select":
        return move, _select_action(kind, move.select_index)
    if can_skip and selected_count == 0:
        return move, _skip_action(kind)
    if met_min:
        return move, _confirm_action(kind)
    forced = _forced_selection_move(selection)
    if forced is not None:
        return forced, _select_action(kind, forced.select_index)
    return move, None


def wait_settled(
    client: ModClient,
    *,
    timeout_s: float = DEFAULT_SETTLE_TIMEOUT_S,
    poll_s: float = DEFAULT_POLL_S,
) -> dict:
    """Poll until the mod reports a settled position, or return the last state read
    once `timeout_s` runs out -- the caller (`MovePlayer`, or a non-combat navigator)
    decides what an unsettled return means."""
    deadline = time.monotonic() + timeout_s
    state = client.get_state()
    while not is_settled(state):
        if time.monotonic() > deadline:
            return state
        time.sleep(poll_s)
        state = client.get_state()
    return state


@dataclass
class PlayStats:
    moves: int = 0
    checks: int = 0
    check_errors: int = 0


@dataclass
class FightOutcome:
    state: dict  # the first settled non-combat state once the fight ended
    ok: bool  # False: play_fight gave up (a refused move, a snapshot it can't read, stuck)
    last_hp: int | None = None  # the player's hp at the last combat state seen
    # True only for the no-progress give-up (auto-kill off, unavailable, or itself
    # failed): this fight cannot be finished, so the caller (coach.autopilot._play_run)
    # should abandon the run instead of retrying the same stuck position forever. False
    # (default) for every other ok=False case, e.g. a transient settle-timeout blip,
    # which the existing retry-by-calling-play_fight-again behavior still covers.
    gave_up: bool = False


class MovePlayer:
    """Analyzes each settled combat decision with the engine, plays its best move
    through the mod, and fidelity-checks the transition once the next position
    settles."""

    def __init__(
        self,
        client: ModClient,
        engine: EngineClient,
        runlog: RunLog,
        *,
        time_budget_s: float = 1.5,
        game_version: str = "unknown",
        settle_timeout_s: float = DEFAULT_SETTLE_TIMEOUT_S,
        poll_s: float = DEFAULT_POLL_S,
        source: str = "fightlab",
        no_progress_rounds: int = DEFAULT_NO_PROGRESS_ROUNDS,
        jam_timeout_s: float = DEFAULT_JAM_TIMEOUT_S,
        auto_kill_no_progress: bool = False,
    ) -> None:
        self._client = client
        self._engine = engine
        self._runlog = runlog
        self._time_budget_s = time_budget_s
        self._game_version = game_version
        self._settle_timeout_s = settle_timeout_s
        self._poll_s = poll_s
        self._source = source
        self._no_progress_rounds = no_progress_rounds
        self._jam_timeout_s = jam_timeout_s
        # Off by default: fight-lab (which never passes this) keeps its exact existing
        # give-up behavior. coach.autopilot.run turns it on for its own MovePlayer, so a
        # wall like The Insatiable is cleared instead of ending the whole run (spec).
        self._auto_kill_no_progress = auto_kill_no_progress
        self._jam_fingerprint: str | None = None
        self._jam_stuck_since: float = 0.0
        self._last_room_type: str | None = None
        self.stats = PlayStats()

    def play_fight(self, state: dict, *, max_moves: int = DEFAULT_MAX_MOVES) -> FightOutcome:
        """Play settled combat decisions from `state` until the fight ends (the mod's
        `state_type` leaves the combat set), `max_moves` is hit, or no-progress gives up
        first (`_no_progress_rounds` full rounds without a new low in total alive-enemy
        hp -- a wall like The Insatiable or Aeonglass/Entomancer, not a slow win like
        the Decimillipede, whose segments can reattach for a few rounds at a time but
        which this is calibrated to outlast; see DEFAULT_NO_PROGRESS_ROUNDS).

        With `auto_kill_no_progress` on (the autopilot's own MovePlayer), reaching that
        cap calls `debug_kill_enemy` instead of giving up, and keeps fighting whatever
        follows (a next boss stage); only a failed/refused kill (`allow_debug_actions`
        off) gives up, with `FightOutcome.gave_up` set so the caller abandons the run
        instead of retrying the same stuck fight forever.

        Separately, `end_turn` sent twice in a row with no round/turn change (a
        multi-stage boss's first stage dying left it ignored live, 2026-09-29) triggers
        its own recovery: `debug_force_play_phase` then `end_turn` again, and
        `debug_kill_enemy` as a last resort if that still does not move it."""
        pending: tuple[dict, Move, str] | None = None
        last_hp = _hp_of(state)
        best_enemy_hp: int | None = None
        stalled_rounds = 0
        last_round_checked: int | None = None
        end_turn_stalls = 0  # consecutive end_turn sends with no round/turn change
        for _ in range(max_moves):
            if (hp := _hp_of(state)) is not None:
                last_hp = hp
            if not in_combat(state):
                if pending is not None:
                    self._resolve_check(pending, None, None, comparable=False)
                return FightOutcome(state=state, ok=True, last_hp=last_hp)
            if not is_settled(state):
                settled = wait_settled(
                    self._client, timeout_s=self._settle_timeout_s, poll_s=self._poll_s
                )
                if not is_settled(settled) and in_combat(settled):
                    current_fp = fingerprint(settled)
                    if current_fp != self._jam_fingerprint:
                        self._jam_fingerprint = current_fp
                        self._jam_stuck_since = time.monotonic()
                    elif time.monotonic() - self._jam_stuck_since >= self._jam_timeout_s:
                        message = (
                            "jam: is_resolving has stayed true with no state change "
                            f"for about {self._jam_timeout_s:.0f}s -- the game is not "
                            "responding to input (confirmed live: only a game restart "
                            "clears this)"
                        )
                        self._runlog.append("error", {"error": message})
                        raise GameJamError(message)
                    return FightOutcome(state=settled, ok=False, last_hp=last_hp)
                self._jam_fingerprint = None
                state = settled
                continue
            decision = classify(state)
            key = fingerprint(state)
            self._log_state(state, decision, key)
            if decision.kind is DecisionKind.COMBAT:
                round_ = (state.get("battle") or {}).get("round")
                if isinstance(round_, int) and round_ != last_round_checked:
                    last_round_checked = round_
                    hp = _total_enemy_hp(state)
                    if best_enemy_hp is None or hp < best_enemy_hp:
                        best_enemy_hp, stalled_rounds = hp, 0
                    else:
                        stalled_rounds += 1
                    if stalled_rounds > self._no_progress_rounds:
                        if pending is not None:
                            self._resolve_check(pending, None, None, comparable=False)
                            pending = None
                        if self._auto_kill_no_progress and self._post_debug_kill_enemy(
                            reason="no_progress"
                        ):
                            # A fresh baseline for whatever follows (a next boss stage,
                            # or the fight just ending) -- not a comparison against the
                            # wall that was just cleared.
                            best_enemy_hp, stalled_rounds, last_round_checked = None, 0, None
                            state = wait_settled(
                                self._client, timeout_s=self._settle_timeout_s, poll_s=self._poll_s
                            )
                            continue
                        self._runlog.append(
                            "error",
                            {
                                "error": (
                                    f"fightlab: no progress -- total enemy hp has not "
                                    f"reached a new low ({best_enemy_hp}) in "
                                    f"{stalled_rounds} turns"
                                )
                            },
                        )
                        return FightOutcome(
                            state=state, ok=False, last_hp=last_hp, gave_up=True
                        )
            battle = state.get("battle") or {}
            pre_end_turn_signature = (battle.get("round"), battle.get("turn"))
            if pending is not None:
                self._resolve_check(pending, state, key, comparable=True)
                pending = None
            outcome = self._act(state, decision, key)
            if outcome is None:
                return FightOutcome(state=state, ok=False, last_hp=last_hp)
            live, move = outcome
            pending = (live, move, key)
            state = wait_settled(
                self._client, timeout_s=self._settle_timeout_s, poll_s=self._poll_s
            )
            if move.kind == "end_turn" and in_combat(state):
                post_battle = state.get("battle") or {}
                post_signature = (post_battle.get("round"), post_battle.get("turn"))
                if post_signature == pre_end_turn_signature:
                    end_turn_stalls += 1
                else:
                    end_turn_stalls = 0
                if end_turn_stalls >= END_TURN_STALL_THRESHOLD:
                    state = self._recover_stalled_end_turn(state, post_signature)
                    end_turn_stalls = 0
            else:
                end_turn_stalls = 0
        self._runlog.append(
            "error", {"error": f"fightlab: {max_moves} moves without the fight ending"}
        )
        return FightOutcome(state=state, ok=False, last_hp=last_hp)

    # --- one decision ------------------------------------------------------------

    def _room_type_for(self, decision: Decision) -> str | None:
        if decision.kind is DecisionKind.COMBAT:
            self._last_room_type = decision.room_type
        return self._last_room_type

    def _log_state(self, state: dict, decision: Decision, key: str) -> None:
        self._runlog.append(
            "state",
            {
                "fingerprint": key,
                "state": state,
                "decision": {
                    "kind": decision.kind.value,
                    "state_type": decision.state_type,
                    "room_type": decision.room_type,
                },
            },
        )

    def _act(self, state: dict, decision: Decision, key: str) -> tuple[dict, Move] | None:
        room_type = self._room_type_for(decision)
        try:
            live = build_live_snapshot(
                state, game_version=self._game_version, room_type=room_type, warnings=[]
            )
        except SnapshotError as exc:
            self._runlog.append("analysis", {"fingerprint": key, "error": f"snapshot: {exc}"})
            return None
        try:
            analysis = self._engine.analyze(live, time_budget_s=self._time_budget_s)
        except EngineError as exc:
            self._runlog.append("analysis", {"fingerprint": key, "error": str(exc)})
            if decision.kind is DecisionKind.COMBAT_SELECT:
                self._fallback_selection(live)
            return None
        self._runlog.append(
            "analysis",
            {
                "fingerprint": key,
                "current": True,
                "live": live,
                "analysis": dataclasses.asdict(analysis),
            },
        )
        move = playable_choice(state, analysis)
        if decision.kind is DecisionKind.COMBAT_SELECT:
            selection = live.get("pending_selection") or {}
            move, action = _selection_step(selection, move)
        else:
            action = move_to_action(move)
        if action is None:
            self._runlog.append(
                "action", {"error": f"no mod action for move kind {move.kind!r}"}
            )
            return None
        try:
            self._client.post_action(action)
        except BridgeError as exc:
            self._runlog.append(
                "action", {"move": dataclasses.asdict(move), "error": str(exc)}
            )
            return None
        self._runlog.append(
            "action", {"move": dataclasses.asdict(move), "source": self._source}
        )
        self.stats.moves += 1
        return live, move

    def _post_debug_kill_enemy(self, *, reason: str) -> bool:
        """Kill every living enemy through the mod's own command path (STS2MCP
        McpMod.Debug.cs `ExecuteDebugKillEnemy` -> `CreatureCmd.Kill`), so a fight this
        player cannot win still finishes: death, any multi-stage boss's next-stage
        transition, and reward logic all run exactly as a real kill would. Gated by
        `allow_debug_actions` on the mod side; a `BridgeError` there (debug actions off,
        or nothing left alive to kill) is logged and reported as failure, not raised --
        the caller falls back to letting the fight/run end. Logged as its own kind
        "debug" session record (with the action), so the fidelity tools can see a
        debug-assisted fight."""
        try:
            self._client.post_action({"action": "debug_kill_enemy"})
        except BridgeError as exc:
            self._runlog.append(
                "error", {"error": f"autoplay: debug_kill_enemy failed ({reason}): {exc}"}
            )
            return False
        self._runlog.append("debug", {"action": "debug_kill_enemy", "reason": reason})
        return True

    def _recover_stalled_end_turn(self, state: dict, stalled_signature: tuple) -> dict:
        """`end_turn` was ignored `END_TURN_STALL_THRESHOLD` times in a row (no
        round/turn change -- confirmed live after a multi-stage boss's first stage
        died, 2026-09-29, while a manual click worked). Try the mod's own
        `debug_force_play_phase` (best-effort: it only reports readiness, and does
        nothing if debug actions are off) and send `end_turn` again; `debug_kill_enemy`
        is the last resort, only if the position is still exactly where it was stuck."""
        self._runlog.append(
            "error",
            {"error": "autoplay: end_turn ignored twice in a row; attempting recovery"},
        )
        try:
            self._client.post_action({"action": "debug_force_play_phase"})
        except BridgeError as exc:
            self._runlog.append(
                "error", {"error": f"autoplay: debug_force_play_phase failed: {exc}"}
            )
        else:
            self._runlog.append(
                "debug", {"action": "debug_force_play_phase", "reason": "end_turn_stall"}
            )
        try:
            self._client.post_action({"action": "end_turn"})
        except BridgeError as exc:
            self._runlog.append(
                "error", {"error": f"autoplay: recovery end_turn failed: {exc}"}
            )
        else:
            self._runlog.append(
                "action", {"move": {"kind": "end_turn"}, "source": "recovery"}
            )
            self.stats.moves += 1
        state = wait_settled(self._client, timeout_s=self._settle_timeout_s, poll_s=self._poll_s)
        if in_combat(state):
            battle = state.get("battle") or {}
            if (battle.get("round"), battle.get("turn")) == stalled_signature:
                self._post_debug_kill_enemy(reason="end_turn_stall")
                state = wait_settled(
                    self._client, timeout_s=self._settle_timeout_s, poll_s=self._poll_s
                )
        return state

    def _fallback_selection(self, live: dict) -> None:
        """Act on a selection the engine could not read, by the screen's own rules.

        Live (2026-09-29): on an "any number" screen a picked card leaves the candidate
        list and the converter refused the state, so no decision came back and the
        screen spun until the 4000-screen cap. `_selection_step` with no engine pick
        confirms once the minimum is met (nothing, on an any-number screen), skips when
        allowed, and otherwise forces the first candidate. No check is recorded: the
        action is not the engine's."""
        selection = live.get("pending_selection") or {}
        move, action = _selection_step(selection, _NO_ENGINE_MOVE)
        if action is None:
            return
        try:
            self._client.post_action(action)
        except BridgeError as exc:
            self._runlog.append("action", {"move": dataclasses.asdict(move), "error": str(exc)})
            return
        self._runlog.append("action", {"move": dataclasses.asdict(move), "source": "fallback"})

    def _resolve_check(
        self,
        pending: tuple[dict, Move, str],
        post_state: dict | None,
        post_key: str | None,
        *,
        comparable: bool,
    ) -> None:
        pre_live, move, pre_key = pending
        if not comparable or post_state is None:
            self._runlog.append(
                "check",
                {
                    "pre_fingerprint": pre_key,
                    "move": dataclasses.asdict(move),
                    "skipped": "the fight ended on this move",
                },
            )
            return
        try:
            post_live = build_live_snapshot(
                post_state,
                game_version=self._game_version,
                room_type=self._last_room_type,
                warnings=[],
            )
        except SnapshotError as exc:
            self._runlog.append(
                "check",
                {
                    "pre_fingerprint": pre_key,
                    "move": dataclasses.asdict(move),
                    "skipped": f"post snapshot: {exc}",
                },
            )
            return
        try:
            result = self._engine.check(pre_live, move, post_live)
        except EngineError as exc:
            self._runlog.append(
                "check",
                {"pre_fingerprint": pre_key, "move": dataclasses.asdict(move), "error": str(exc)},
            )
            self.stats.check_errors += 1
            return
        self._runlog.append(
            "check",
            {
                "pre_fingerprint": pre_key,
                "post_fingerprint": post_key,
                "move": dataclasses.asdict(move),
                "result": dataclasses.asdict(result),
            },
        )
        self.stats.checks += 1


def _hp_of(state: dict) -> int | None:
    hp = (state.get("player") or {}).get("hp")
    return hp if isinstance(hp, int) and not isinstance(hp, bool) else None


def _total_enemy_hp(state: dict) -> int:
    """Sum of every alive enemy's hp, for the no-progress check. 0 counts as real
    progress (every enemy dead), not a missing reading -- only skipped/malformed
    entries are left out."""
    total = 0
    for enemy in (state.get("battle") or {}).get("enemies") or []:
        if not isinstance(enemy, dict) or not enemy.get("alive"):
            continue
        hp = enemy.get("hp")
        if isinstance(hp, int) and not isinstance(hp, bool):
            total += hp
    return total
