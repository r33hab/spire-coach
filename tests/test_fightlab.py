"""Tests for coach/fightlab.py: the fight lab's own setup/play/recovery loop, against
the ScriptedMod fake and a stub move player (coach.autoplay.MovePlayer is tested on
its own in test_autoplay.py; here only the orchestration around it matters)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fakes.fake_mod import ScriptedMod

from coach import autoplay, fightlab
from coach.autoplay import FightOutcome
from coach.bridge.client import BridgeError
from coach.deckseed import SampledCard
from coach.encounters import ENCOUNTERS, Encounter
from coach.runlog import RunLog


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # ScriptedMod answers every poll correctly on its first read, so nothing here
    # should ever actually wait out a poll_s/timeout_s; this just guards against a
    # test bug turning into a real 30s wait per unmatched predicate.
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


@dataclass
class _Scripted:
    outcome: FightOutcome
    moves: int = 1


class StubPlayer:
    """Stands in for `coach.autoplay.MovePlayer`: no real combat, just a scripted
    outcome per call and a `stats.moves` counter that behaves the way the real
    player's does (a running total `fightlab._play_one` diffs across the call)."""

    def __init__(self, script: list[_Scripted]) -> None:
        self._script = list(script)
        self._i = 0
        self.stats = SimpleNamespace(moves=0)
        self.seen_states: list[dict] = []

    def play_fight(self, state: dict, **kwargs) -> FightOutcome:
        self.seen_states.append(state)
        item = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        self.stats.moves += item.moves
        return item.outcome


def won(hp: int = 50) -> FightOutcome:
    return FightOutcome(state={"state_type": "rewards", "player": {"hp": hp}}, ok=True, last_hp=hp)


def died() -> FightOutcome:
    return FightOutcome(state={"state_type": "game_over", "player": {"hp": 0}}, ok=True, last_hp=0)


def make_mod() -> ScriptedMod:
    mod = ScriptedMod(current_profile_id=2)
    mod.encounter_tiers = {e.name: e.tier for e in ENCOUNTERS}
    return mod


def run_lab(mod, player, tmp_path, *, fights=1, acts=(2, 3, 1), buffer_hp=0):
    runlog = RunLog(tmp_path, subdir="runs", prefix="fightlab-")
    summary = fightlab.run(
        mod,
        object(),  # engine: never touched, MovePlayer is stubbed
        runlog,
        fights=fights,
        acts=acts,
        buffer_hp=buffer_hp,
        move_player=player,
        shards_root=tmp_path / "no-shards",
    )
    return summary, runlog


def test_run_plays_the_asked_number_of_fights_and_counts_wins(tmp_path):
    mod = make_mod()
    player = StubPlayer([_Scripted(won(), moves=3), _Scripted(won(), moves=2)])

    summary, runlog = run_lab(mod, player, tmp_path, fights=2)

    assert summary.fights_played == 2
    assert summary.wins == 2
    assert summary.deaths == 0
    assert summary.moves_recorded == 5
    assert runlog.path.exists()


def test_run_counts_a_death_and_still_returns_to_the_menu(tmp_path):
    mod = make_mod()
    player = StubPlayer([_Scripted(died())])

    summary, _ = run_lab(mod, player, tmp_path, fights=1)

    assert (summary.wins, summary.deaths) == (0, 1)
    left_cleanly = {"action": "return_to_main_menu"} in mod.posted or any(
        a.get("option") == "main_menu" for a in mod.posted
    )
    assert left_cleanly


def test_run_advances_to_the_right_act_before_the_encounter(tmp_path, monkeypatch):
    mod = make_mod()
    player = StubPlayer([_Scripted(won())])
    forced = [Encounter("KaiserCrabBoss", 2, "Hive", "boss")]
    monkeypatch.setattr(fightlab, "rank_encounters", lambda *a, **k: forced)

    run_lab(mod, player, tmp_path, fights=1)

    assert sum(1 for a in mod.posted if a.get("action") == "debug_enter_next_act") == 1
    encounter_calls = [a for a in mod.posted if a.get("action") == "debug_start_encounter"]
    assert encounter_calls == [{"action": "debug_start_encounter", "encounter": "KaiserCrabBoss"}]


def test_buffer_hp_applies_to_a_boss_fight_only(tmp_path, monkeypatch):
    # ScriptedMod's default max_hp is 80 (fake_mod.py), so a target of 500 tops up by
    # 420 -- debug_gain_max_hp's own "amount" is a delta (bug 2/7), not the target.
    mod = make_mod()
    player = StubPlayer([_Scripted(won())])
    forced = [Encounter("KaiserCrabBoss", 2, "Hive", "boss")]
    monkeypatch.setattr(fightlab, "rank_encounters", lambda *a, **k: forced)

    _, runlog = run_lab(mod, player, tmp_path, fights=1, buffer_hp=500)

    assert {"action": "debug_gain_max_hp", "amount": 420} in mod.posted
    lines = [json.loads(line) for line in runlog.path.read_text(encoding="utf-8").splitlines()]
    assert any(
        line["kind"] == "buffer" and line["payload"]["target_hp"] == 500 for line in lines
    )


def test_buffer_hp_does_not_stack_across_repeated_boss_fights(tmp_path, monkeypatch):
    # Bug 2/7: --buffer-hp H used to ADD H every fight (reached 110,080 with
    # H=10000). A fake mod that applies the delta like the real one does (current
    # AND max HP both raised): a second boss fight must send nothing further, since
    # max_hp already meets the target the first fight reached.
    class BufferTrackingMod(ScriptedMod):
        def post_action(self, action, **kwargs):
            result = super().post_action(action, **kwargs)
            if action.get("action") == "debug_gain_max_hp":
                self.max_hp += action["amount"]
                self.hp += action["amount"]
            return result

    mod = BufferTrackingMod(current_profile_id=2)
    mod.encounter_tiers = {e.name: e.tier for e in ENCOUNTERS}
    player = StubPlayer([_Scripted(won()), _Scripted(won())])
    forced = [Encounter("KaiserCrabBoss", 2, "Hive", "boss")]
    monkeypatch.setattr(fightlab, "rank_encounters", lambda *a, **k: forced)

    run_lab(mod, player, tmp_path, fights=2, buffer_hp=500)

    buffer_actions = [a for a in mod.posted if a.get("action") == "debug_gain_max_hp"]
    assert buffer_actions == [{"action": "debug_gain_max_hp", "amount": 420}]
    assert mod.max_hp == 500


def test_buffer_hp_does_not_apply_to_a_plain_monster(tmp_path, monkeypatch):
    mod = make_mod()
    player = StubPlayer([_Scripted(won())])
    forced = [Encounter("BowlbugsWeak", 2, "Hive", "monster")]
    monkeypatch.setattr(fightlab, "rank_encounters", lambda *a, **k: forced)

    run_lab(mod, player, tmp_path, fights=1, buffer_hp=500)

    assert not any(a.get("action") == "debug_gain_max_hp" for a in mod.posted)


def test_run_recovers_from_a_bridge_error_and_counts_a_failure(tmp_path):
    mod = make_mod()
    real_post = ScriptedMod.post_action

    def refusing_post(self, action, **kwargs):
        if action.get("action") == "debug_start_encounter":
            self.posted.append(action)
            raise BridgeError("mod is busy")
        return real_post(self, action, **kwargs)

    mod.post_action = refusing_post.__get__(mod, ScriptedMod)
    player = StubPlayer([_Scripted(won())])

    summary, runlog = run_lab(mod, player, tmp_path, fights=1)

    assert summary.fights_played == 0
    assert summary.failures == 1
    lines = runlog.path.read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line)["kind"] == "error" for line in lines)


def test_run_recovers_from_a_game_jam_instead_of_crashing(tmp_path):
    # Bug 3/7: MovePlayer.play_fight now raises GameJamError on a stuck game, which
    # fight-lab shares the move player class with -- it must count this the same way
    # as any other per-fight failure (recover, move on) rather than letting a new
    # exception type it never used to see crash the whole lab run.
    mod = make_mod()

    class JammedPlayer:
        def __init__(self) -> None:
            self.stats = SimpleNamespace(moves=0)

        def play_fight(self, state, **kwargs):
            raise autoplay.GameJamError("jam: is_resolving stuck true with no state change")

    summary, runlog = run_lab(mod, JammedPlayer(), tmp_path, fights=1)

    assert summary.fights_played == 0
    assert summary.failures == 1
    lines = [json.loads(line) for line in runlog.path.read_text(encoding="utf-8").splitlines()]
    assert any(line["kind"] == "error" and "jam" in line["payload"]["error"].lower() for line in lines)


def test_run_stages_a_sampled_deck_when_one_is_found(tmp_path, monkeypatch):
    mod = make_mod()
    player = StubPlayer([_Scripted(won())])
    sampled = [SampledCard("MOLTEN_FIST", True)]
    monkeypatch.setattr(fightlab.deckseed, "sample_deck", lambda *a, **k: sampled)
    staged: list[SampledCard] = []
    monkeypatch.setattr(
        fightlab.deckseed,
        "stage_deck",
        lambda client, cards, **k: staged.extend(cards) or [c.entry_id for c in cards],
    )

    run_lab(mod, player, tmp_path, fights=1)

    assert staged == sampled


def test_run_skips_staging_when_no_deck_is_sampled(tmp_path, monkeypatch):
    mod = make_mod()
    player = StubPlayer([_Scripted(won())])
    monkeypatch.setattr(fightlab.deckseed, "sample_deck", lambda *a, **k: None)
    called: list[int] = []
    monkeypatch.setattr(fightlab.deckseed, "stage_deck", lambda *a, **k: called.append(1))

    run_lab(mod, player, tmp_path, fights=1)

    assert called == []


def test_run_logs_when_staged_cards_are_missing_after_setup(tmp_path, monkeypatch):
    # Item 5 ("verify it from the game state after setup") and the regression fixed in
    # coach.deckseed.stage_deck (pile "draw", not the mod's "deck" option): debug_add_card
    # can answer "ok" for every sampled card and still leave none of them reachable by
    # this fight. ScriptedMod's default combat state never carries hand/draw_pile/etc,
    # so every staged card here comes up missing -- the same shape a real fight showed
    # when cards landed in the wrong pile.
    mod = make_mod()
    player = StubPlayer([_Scripted(won())])
    sampled = [SampledCard("MOLTEN_FIST", True)]
    monkeypatch.setattr(fightlab.deckseed, "sample_deck", lambda *a, **k: sampled)
    monkeypatch.setattr(fightlab.deckseed, "stage_deck", lambda *a, **k: ["MOLTEN_FIST"])

    _, runlog = run_lab(mod, player, tmp_path, fights=1)

    lines = [json.loads(line) for line in runlog.path.read_text(encoding="utf-8").splitlines()]
    errors = [line for line in lines if line["kind"] == "error"]
    assert any("MOLTEN_FIST" in e["payload"]["error"] for e in errors)


def test_run_does_not_warn_when_staged_cards_are_found_after_setup(tmp_path, monkeypatch):
    mod = make_mod()
    mod.extra_state = {
        "player": {
            "hp": 80,
            "max_hp": 80,
            "hand": [{"id": "MOLTEN_FIST"}],
            "draw_pile": [],
            "discard_pile": [],
            "exhaust_pile": [],
        }
    }
    player = StubPlayer([_Scripted(won())])
    sampled = [SampledCard("MOLTEN_FIST", True)]
    monkeypatch.setattr(fightlab.deckseed, "sample_deck", lambda *a, **k: sampled)
    monkeypatch.setattr(fightlab.deckseed, "stage_deck", lambda *a, **k: ["MOLTEN_FIST"])

    _, runlog = run_lab(mod, player, tmp_path, fights=1)

    lines = [json.loads(line) for line in runlog.path.read_text(encoding="utf-8").splitlines()]
    assert [line for line in lines if line["kind"] == "error"] == []


def test_session_header_records_loaded_mods_when_the_mod_reports_them(tmp_path):
    mod = make_mod()
    mod.loaded_mods = [{"id": "watcher", "name": "Watcher", "affects_gameplay": True}]
    player = StubPlayer([_Scripted(won())])

    _, runlog = run_lab(mod, player, tmp_path, fights=1)

    first = json.loads(runlog.path.read_text(encoding="utf-8").splitlines()[0])
    assert first["kind"] == "session"
    assert first["payload"]["loaded_mods"] == mod.loaded_mods


def test_session_header_is_none_when_the_mod_never_reports_it(tmp_path):
    mod = make_mod()  # ScriptedMod defaults loaded_mods to None
    player = StubPlayer([_Scripted(won())])

    _, runlog = run_lab(mod, player, tmp_path, fights=1)

    first = json.loads(runlog.path.read_text(encoding="utf-8").splitlines()[0])
    assert first["payload"]["loaded_mods"] is None
