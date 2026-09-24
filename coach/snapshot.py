"""Build a live snapshot (`sts2-live-snapshot/1`) from the mod's combat state JSON.

This is the fairness boundary between the game and the simulator: every
output field is read from the mod payload by an explicit, named allowlist,
never by copying a dict wholesale. A hidden-information field the mod may
carry (`draw_pile_ordered`, `move_follow_up_id`, RNG, pools, unseen
encounters) is therefore never even looked at, let alone leaked through.

See docs/superpowers/specs/2026-09-24-snapshot-schemas.md section 1 for the
accepted shape, and
.superpowers/sdd/2026-09-24-stage0-1a-toolchain-mod-capture/task-11-addendum.md
for the exact per-field adapter rules this module follows.
"""

from __future__ import annotations

SCHEMA = "sts2-live-snapshot/1"

_COMBAT_ROOM_TYPES = frozenset({"monster", "elite", "boss"})
_ATTACK_INTENT_TYPES = frozenset({"Attack", "DeathBlow"})
_PRIMITIVE_TYPES = (bool, int, str)  # bool before int; docs never mention floats

# battle.history: of the 7 keys the converter accepts, only these 4 are ever
# derivable from the mod's payload (task-11-addendum.md rule 1). The other 3
# (cards_drawn_this_combat, cards_generated_this_combat,
# lightning_orbs_channeled_this_combat) are always omitted -- omitted means
# unknown, never 0. Every other mod history key (e.g. powers_played_this_turn)
# is dropped.
_HISTORY_KEYS = (
    "cards_played_this_turn",
    "cards_played_this_combat",
    "attacks_played_this_turn",
    "skills_played_this_turn",
)

# Card `fields`: the only two keys the converter understands
# (task-11-addendum.md rule 2). `cost_until_played` and anything else is
# dropped -- the converter would warn on it.
_COST_FIELD_KEYS = ("cost_this_turn", "cost_for_combat")


class SnapshotError(Exception):
    """A required field was missing or invalid, at the given dotted JSON path."""

    def __init__(self, path: str, detail: str | None = None) -> None:
        self.path = path
        message = f"missing or invalid required field: {path}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)


def _require(container: dict, key: str, path: str):
    """The value at container[key], or raise SnapshotError(path) if it's absent or null."""
    if not isinstance(container, dict) or container.get(key) is None:
        raise SnapshotError(path)
    return container[key]


def _strip_category_prefix(value: str) -> str:
    """`CHARACTER.IRONCLAD` -> `IRONCLAD`. A value with no prefix passes through."""
    return value.rsplit(".", 1)[-1]


def _strip_entity_suffix(entity_id: str) -> str:
    """`JAW_WORM_0` -> `JAW_WORM`."""
    return entity_id.rsplit("_", 1)[0]


def _primitive_fields(fields: dict | None) -> dict:
    """Keep only bool/int/str values; drop lists, dicts, None and anything else."""
    if not fields:
        return {}
    return {k: v for k, v in fields.items() if isinstance(v, _PRIMITIVE_TYPES)}


def _build_cost(raw_cost, *, path: str) -> int | None:
    """The mod's cost is a string: "X" and any unplayable (negative) cost become null."""
    if raw_cost is None or raw_cost == "X":
        return None
    try:
        value = int(raw_cost)
    except (TypeError, ValueError) as exc:
        raise SnapshotError(path, f"unparseable cost {raw_cost!r}") from exc
    return None if value < 0 else value


def _build_card_fields(fields: dict | None) -> dict:
    """Only cost_this_turn / cost_for_combat, each only when the mod set it."""
    if not fields:
        return {}
    return {key: fields[key] for key in _COST_FIELD_KEYS if key in fields}


def _build_card(card: dict, *, path: str) -> dict:
    enchantment = card.get("enchantment")
    return {
        "id": _require(card, "id", f"{path}.id"),
        "upgraded": _require(card, "is_upgraded", f"{path}.is_upgraded"),
        "uid": card.get("uid"),
        "cost": _build_cost(card.get("cost"), path=f"{path}.cost"),
        "enchantment": enchantment.get("id") if enchantment else None,
        "enchant_amount": enchantment.get("amount") if enchantment else None,
        "fields": _build_card_fields(card.get("fields")),
    }


def _build_cards(cards, *, path: str) -> list[dict]:
    return [_build_card(c, path=f"{path}[{i}]") for i, c in enumerate(cards or [])]


def _build_power(power: dict, *, path: str) -> dict:
    return {
        "id": _require(power, "id", f"{path}.id"),
        "amount": _require(power, "amount", f"{path}.amount"),
    }


def _build_powers(powers, *, path: str) -> list[dict]:
    return [_build_power(p, path=f"{path}[{i}]") for i, p in enumerate(powers or [])]


def _build_relic(relic: dict, *, path: str) -> dict:
    merged = {**_primitive_fields(relic.get("fields")), **_primitive_fields(relic.get("saved"))}
    return {
        "id": _require(relic, "id", f"{path}.id"),
        "counter": relic.get("counter"),
        "fields": merged,
    }


def _build_orbs(player: dict) -> dict:
    orbs = player.get("orbs") or []
    return {
        "slots": player.get("orb_slots", 0),
        "list": [
            {
                "id": _require(orb, "id", f"player.orbs[{i}].id"),
                "passive": orb.get("passive_val"),
                "evoke": orb.get("evoke_val"),
            }
            for i, orb in enumerate(orbs)
        ],
    }


def _build_potions(player: dict) -> list[dict]:
    max_slots = player.get("max_potion_slots") or 0
    slots: list[dict] = [{"slot": i, "id": None} for i in range(max_slots)]
    for i, potion in enumerate(player.get("potions") or []):
        slot = _require(potion, "slot", f"player.potions[{i}].slot")
        if 0 <= slot < max_slots:
            slots[slot]["id"] = _require(potion, "id", f"player.potions[{i}].id")
    return slots


def _build_player(player: dict) -> dict:
    return {
        "hp": _require(player, "hp", "player.hp"),
        "max_hp": _require(player, "max_hp", "player.max_hp"),
        "block": _require(player, "block", "player.block"),
        "energy": _require(player, "energy", "player.energy"),
        "max_energy": _require(player, "max_energy", "player.max_energy"),
        "gold": _require(player, "gold", "player.gold"),
        "stars": player.get("stars", 0),
        "osty": None,  # Necrobinder pets are out of scope; see task-11-report.md.
        "orbs": _build_orbs(player),
        "powers": _build_powers(player.get("status"), path="player.status"),
        "relics": [
            _build_relic(r, path=f"player.relics[{i}]")
            for i, r in enumerate(player.get("relics") or [])
        ],
        "potions": _build_potions(player),
        "max_potion_slots": _require(player, "max_potion_slots", "player.max_potion_slots"),
    }


def _build_hand(player: dict) -> list[dict]:
    hand = _require(player, "hand", "player.hand")
    ordered = sorted(hand, key=lambda c: c.get("index", 0))
    return [_build_card(c, path=f"hand[{c.get('index')}]") for c in ordered]


def _build_draw_pile(player: dict) -> list[dict]:
    """draw_pile is an UNORDERED multiset, sorted by (id, upgraded) for stability.

    Built only from player.draw_pile -- player.draw_pile_ordered (the true
    draw order) is hidden information and is never read here.
    """
    draw_pile = _require(player, "draw_pile", "player.draw_pile")
    cards = _build_cards(draw_pile, path="draw_pile")
    return sorted(cards, key=lambda c: (c["id"], c["upgraded"]))


def _build_ordered_pile(
    player: dict, *, unordered_key: str, ordered_key: str, path: str
) -> list[dict]:
    """discard_pile / exhaust_pile: true pile order comes from *_ordered when present.

    *_ordered entries are the minimal {id, is_upgraded, uid} shape; each is
    matched by uid to its full Pile Card Object in the unordered list to
    recover cost/enchantment/fields. Falls back to the unordered list's own
    order if *_ordered is absent from the payload.
    """
    unordered = _require(player, unordered_key, f"player.{unordered_key}")
    ordered = player.get(ordered_key)
    if not ordered:
        return _build_cards(unordered, path=path)

    by_uid = {c.get("uid"): c for c in unordered}
    result = []
    for i, entry in enumerate(ordered):
        uid = entry.get("uid")
        card = by_uid.get(uid)
        if card is None:
            raise SnapshotError(
                f"player.{ordered_key}[{i}].uid", f"no matching entry in player.{unordered_key}"
            )
        result.append(_build_card(card, path=f"{path}[{i}]"))
    return result


def _build_intent(intent: dict, *, path: str) -> dict:
    raw_type = _require(intent, "type", f"{path}.type")
    is_attack = raw_type in _ATTACK_INTENT_TYPES
    return {
        "type": raw_type.upper(),
        "damage": intent.get("damage") if is_attack else None,
        "base_damage": intent.get("base_damage") if is_attack else None,
        "hits": intent.get("hits") if is_attack else None,
    }


def _build_enemy(enemy: dict, *, path: str) -> dict | None:
    """None when the enemy should be dropped (dead, no revive)."""
    if not enemy.get("alive", False) and not enemy.get("revive"):
        return None

    entity_id = _require(enemy, "entity_id", f"{path}.entity_id")
    return {
        "entity_id": entity_id,
        "id": _strip_entity_suffix(entity_id),
        "slot": enemy.get("slot"),
        "hp": _require(enemy, "hp", f"{path}.hp"),
        "max_hp": _require(enemy, "max_hp", f"{path}.max_hp"),
        "block": _require(enemy, "block", f"{path}.block"),
        "powers": _build_powers(enemy.get("status"), path=f"{path}.status"),
        "intents": [
            _build_intent(intent, path=f"{path}.intents[{i}]")
            for i, intent in enumerate(enemy.get("intents") or [])
        ],
        "move_id": enemy.get("move_id"),
        "state_log": enemy.get("state_log"),
        "ai_fields": _primitive_fields(enemy.get("ai_fields")),
    }


def _build_enemies(battle: dict) -> list[dict]:
    enemies = battle.get("enemies") or []
    built = (_build_enemy(e, path=f"enemies[{i}]") for i, e in enumerate(enemies))
    return [e for e in built if e is not None]


def _build_history(battle: dict) -> dict:
    history = battle.get("history") or {}
    return {
        key: history[key]
        for key in _HISTORY_KEYS
        if key in history and history[key] is not None
    }


def _build_pending_selection(state: dict) -> dict | None:
    if state.get("state_type") != "hand_select":
        return None

    hand_select = _require(state, "hand_select", "hand_select")
    mode = _require(hand_select, "mode", "hand_select.mode")
    return {
        "kind": mode.upper(),
        # The mod's hand_select payload does not report which card triggered
        # the prompt (no source_card / selection-kind field exists yet on
        # this schema-4 shape). See task-11-report.md concerns.
        "source_card": None,
        "candidates": _build_cards(hand_select.get("cards"), path="hand_select.cards"),
        # The mod does not report min/max select counts either; 1/1 covers
        # the overwhelming majority of single-card hand_select prompts.
        "min": 1,
        "max": 1,
        # selected_cards carries {index, name} only, never a uid, so there is
        # nothing to report here. The schema's own comment allows this
        # ("if the mod reports it").
        "selected_uids": [],
    }


def _resolve_room_type(state: dict, room_type: str | None) -> str:
    resolved = room_type or state.get("state_type")
    if resolved not in _COMBAT_ROOM_TYPES:
        raise SnapshotError("room_type", f"got {resolved!r}")
    return resolved


def build_live_snapshot(
    state: dict,
    *,
    game_version: str,
    room_type: str | None = None,
    known_top: list[dict] | None = None,
    known_bottom: list[dict] | None = None,
) -> dict:
    """Build a `sts2-live-snapshot/1` dict from one mod combat state payload.

    `state` is the mod's JSON for a `monster`/`elite`/`boss` state, or a
    `hand_select` state (which still carries the full `battle` object).
    `room_type` must be supplied explicitly for `hand_select` states, since
    their own `state_type` is not one of monster/elite/boss; for a combat
    state it defaults to `state["state_type"]`.

    `known_top` / `known_bottom` are the cards the player knowingly placed on
    top of / bottom of the draw pile (mod Pile Card Object shape), supplied
    by the caller -- the mod does not report this itself.

    Raises SnapshotError, naming the JSON path, for any required field that
    is missing or null.
    """
    run = _require(state, "run", "run")
    player = _require(state, "player", "player")
    battle = _require(state, "battle", "battle")

    resolved_room_type = _resolve_room_type(state, room_type)
    character = _require(player, "character", "player.character")

    return {
        "schema": SCHEMA,
        "game_version": game_version,
        "character": _strip_category_prefix(character),
        "ascension": _require(run, "ascension", "run.ascension"),
        "act": _require(run, "act", "run.act"),
        "floor": _require(run, "floor", "run.floor"),
        "room_type": resolved_room_type,
        "round": _require(battle, "round", "battle.round"),
        "player": _build_player(player),
        "hand": _build_hand(player),
        "draw_pile": _build_draw_pile(player),
        "discard_pile": _build_ordered_pile(
            player,
            unordered_key="discard_pile",
            ordered_key="discard_pile_ordered",
            path="discard_pile",
        ),
        "exhaust_pile": _build_ordered_pile(
            player,
            unordered_key="exhaust_pile",
            ordered_key="exhaust_pile_ordered",
            path="exhaust_pile",
        ),
        "known_draw_top": _build_cards(known_top, path="known_draw_top"),
        "known_draw_bottom": _build_cards(known_bottom, path="known_draw_bottom"),
        "enemies": _build_enemies(battle),
        "history": _build_history(battle),
        "pending_selection": _build_pending_selection(state),
    }
