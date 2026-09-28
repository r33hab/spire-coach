"""Sample a realistic deck for an act from the full-run training's recorded fight
starts, and stage it in the game with `debug_add_card`.

Read-only against `~/Documents/git/sts2-wt`: this only ever opens shard files under
`training/runs/full/*/shards/*.jsonl.gz`, never writes there. Each line is one
self-play run's record; its `fights` list carries the state the run was in when each
fight started, including `state.deck` (the simulator's own numeric card ids) and
`state.act_index` (0-based: Overgrowth/Underdocks = 0, Hive = 1, Glory = 2, matching
`coach.encounters.Encounter.act - 1`). Sim ids are turned into the game's entry ids
with sts2's own converter table (`sts2_gym.live.ids.card_entries`), not re-derived
here, so a rename on that side does not need a matching change here.

Limits, as the spec asks to document:

- Only the card **pool** is approximated, from one real recorded run's deck at a fight
  in the wanted act. Relics, potions, gold and starting energy are not replicated: the
  mod has no `debug_add_relic`/`debug_add_potion`, and reproducing them exactly would
  need RNG state a shard does not carry either.
- Cards are staged into the `deck` pile once combat has started (`debug_add_card`
  needs combat in progress), so they join the fight's future draws and reshuffles;
  they are not guaranteed to be in the turn-1 opening hand.
- A sampled entry's upgrade flag is read as `bool(entry[1])` from the shard's
  `[card_id, upgraded, enchantment_id, reserved]` tuple. That shape is inferred from
  the shards themselves (the training design was not read for this), not confirmed
  against the training code; worst case a card is added unupgraded that was upgraded
  in the sampled run, which is cosmetic, not a fidelity bug.
"""

from __future__ import annotations

import gzip
import json
import random
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from coach.bridge.client import BridgeError, ModClient

DEFAULT_SHARDS_ROOT = Path.home() / "Documents" / "git" / "sts2-wt" / "training" / "runs" / "full"
# The Ironclad's starting deck (STRIKE_IRONCLAD x5, DEFEND_IRONCLAD x4, BASH, and the
# curse a run can start with): already dealt by a fresh encounter, so sampled copies of
# these are not worth re-adding.
_STARTER_ENTRY_IDS = frozenset(
    {"STRIKE_IRONCLAD", "DEFEND_IRONCLAD", "BASH", "ASCENDERS_BANE"}
)


@dataclass(frozen=True)
class SampledCard:
    entry_id: str
    upgraded: bool


def find_shards(root: Path = DEFAULT_SHARDS_ROOT) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(root.glob("gen_*/shards/*.jsonl.gz"))


def iter_fight_starts(shard: Path, *, act_index: int) -> Iterator[dict]:
    """Every fight-start `state` object in `shard` for `act_index`, in file order."""
    with gzip.open(shard, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            for fight in record.get("fights") or []:
                state = fight.get("state")
                if (
                    isinstance(state, dict)
                    and state.get("act_index") == act_index
                    and state.get("deck")
                ):
                    yield state


def sample_deck(
    act: int,
    *,
    shards_root: Path = DEFAULT_SHARDS_ROOT,
    rng: random.Random | None = None,
    max_shards_tried: int = 40,
    card_entries: dict[int, str] | None = None,
) -> list[SampledCard] | None:
    """A realistic run deck for `act` (1-3, the coach's own numbering), or None when no
    shard has a fight start recorded for it. Only the cards beyond the character's
    known starters are returned: a fresh encounter already deals those."""
    rng = rng or random.Random()
    shards = find_shards(shards_root)
    if not shards:
        return None
    rng.shuffle(shards)
    table = card_entries if card_entries is not None else _card_entries()
    for shard in shards[:max_shards_tried]:
        starts = list(iter_fight_starts(shard, act_index=act - 1))
        if not starts:
            continue
        state = rng.choice(starts)
        cards = []
        for entry in state["deck"]:
            if not isinstance(entry, list) or not entry:
                continue
            card_id = entry[0]
            upgraded = bool(entry[1]) if len(entry) > 1 else False
            game_id = table.get(card_id)
            if game_id is None or game_id in _STARTER_ENTRY_IDS:
                continue
            cards.append(SampledCard(game_id, upgraded))
        return cards
    return None


def _card_entries() -> dict[int, str]:
    """sts2's numeric card id -> the game's entry id. Imported lazily so a caller that
    never samples a deck does not need the sts2 checkout importable."""
    from sts2_gym.live import ids  # noqa: PLC0415

    return ids.card_entries()


def stage_deck(client: ModClient, cards: list[SampledCard], *, pile: str = "deck") -> list[str]:
    """`debug_add_card` every sampled card (combat must already be in progress).
    Returns the entry ids the mod accepted; one it refuses (unrecognized on this game
    build) is skipped rather than failing the whole deck."""
    added = []
    for card in cards:
        try:
            client.post_action(
                {
                    "action": "debug_add_card",
                    "card": card.entry_id,
                    "upgraded": card.upgraded,
                    "pile": pile,
                }
            )
        except BridgeError:
            continue
        added.append(card.entry_id)
    return added
