"""Tests for coach/deckseed.py: sampling a realistic deck from the full-run
training's shards (a fake, small one here -- the real ones under
~/Documents/git/sts2-wt are read-only and never touched by a test) and staging it
with debug_add_card."""

from __future__ import annotations

import gzip
import json
import random

from coach.bridge.client import BridgeError
from coach.deckseed import SampledCard, find_shards, iter_fight_starts, sample_deck, stage_deck

CARD_ENTRIES = {1: "STRIKE_IRONCLAD", 2: "DEFEND_IRONCLAD", 100: "BASH", 200: "MOLTEN_FIST"}


def _write_shard(path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _fight(act_index: int, deck: list[list[int]]) -> dict:
    return {"state": {"act_index": act_index, "deck": deck}}


def test_find_shards_globs_every_generation(tmp_path):
    _write_shard(tmp_path / "gen_0000" / "shards" / "r0000.jsonl.gz", [])
    _write_shard(tmp_path / "gen_0001" / "shards" / "r0003.jsonl.gz", [])
    shards = find_shards(tmp_path)
    assert len(shards) == 2


def test_find_shards_missing_root_is_empty(tmp_path):
    assert find_shards(tmp_path / "does-not-exist") == []


def test_iter_fight_starts_filters_by_act_and_requires_a_deck(tmp_path):
    shard = tmp_path / "gen_0000" / "shards" / "r0000.jsonl.gz"
    _write_shard(
        shard,
        [
            {"fights": [_fight(0, [[1, 0]]), _fight(1, [[2, 0]])]},
            {"fights": [_fight(1, [])]},  # no deck: skipped
            {"fights": [_fight(1, [[100, 1]])]},
        ],
    )
    starts = list(iter_fight_starts(shard, act_index=1))
    assert [s["deck"] for s in starts] == [[[2, 0]], [[100, 1]]]


def test_sample_deck_maps_ids_and_drops_starters(tmp_path):
    shard = tmp_path / "gen_0000" / "shards" / "r0000.jsonl.gz"
    _write_shard(
        shard,
        [{"fights": [_fight(1, [[1, 0], [2, 0], [200, 1]])]}],  # act_index 1 = coach's act 2
    )
    cards = sample_deck(2, shards_root=tmp_path, rng=random.Random(0), card_entries=CARD_ENTRIES)
    assert cards == [SampledCard("MOLTEN_FIST", True)]  # starters (Strike/Defend) filtered out


def test_sample_deck_returns_none_when_nothing_matches(tmp_path):
    shard = tmp_path / "gen_0000" / "shards" / "r0000.jsonl.gz"
    _write_shard(shard, [{"fights": [_fight(0, [[1, 0]])]}])
    assert sample_deck(3, shards_root=tmp_path, card_entries=CARD_ENTRIES) is None


def test_sample_deck_no_shards_returns_none(tmp_path):
    assert sample_deck(1, shards_root=tmp_path / "nope") is None


def test_sample_deck_skips_an_unmapped_card_id(tmp_path):
    shard = tmp_path / "gen_0000" / "shards" / "r0000.jsonl.gz"
    _write_shard(shard, [{"fights": [_fight(0, [[999, 0], [200, 0]])]}])
    cards = sample_deck(1, shards_root=tmp_path, card_entries=CARD_ENTRIES)
    assert cards == [SampledCard("MOLTEN_FIST", False)]


class FakeClient:
    def __init__(self, *, refuse: set[str] = frozenset()) -> None:
        self.posted: list[dict] = []
        self._refuse = refuse

    def post_action(self, action: dict, **kwargs) -> dict:
        if action.get("card") in self._refuse:
            raise BridgeError("unknown card")
        self.posted.append(action)
        return {"status": "ok"}


def test_stage_deck_posts_debug_add_card_for_each_card():
    client = FakeClient()
    cards = [SampledCard("MOLTEN_FIST", True), SampledCard("IRON_WAVE", False)]
    added = stage_deck(client, cards)
    assert added == ["MOLTEN_FIST", "IRON_WAVE"]
    assert client.posted == [
        {"action": "debug_add_card", "card": "MOLTEN_FIST", "upgraded": True, "pile": "deck"},
        {"action": "debug_add_card", "card": "IRON_WAVE", "upgraded": False, "pile": "deck"},
    ]


def test_stage_deck_skips_a_refused_card_but_keeps_going():
    client = FakeClient(refuse={"GHOST_CARD"})
    cards = [SampledCard("GHOST_CARD", False), SampledCard("MOLTEN_FIST", False)]
    added = stage_deck(client, cards)
    assert added == ["MOLTEN_FIST"]
