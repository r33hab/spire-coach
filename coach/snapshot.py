"""Build a live snapshot (`sts2-live-snapshot/1`) from the mod's combat state JSON.

This is the fairness boundary between the game and the simulator: every
output field is read from the mod payload by an explicit, named allowlist,
never by copying a dict wholesale, and a field that is missing, null (the
mod's own "could not read this" signal) or the wrong type raises loudly
instead of being defaulted or guessed at. A hidden-information field the mod
may carry (`draw_pile_ordered`, `move_follow_up_id`, `current_run.seed`,
RNG, pools, unseen encounters) is therefore never even looked at, let alone
leaked through -- and an unknown key anywhere in the payload is simply never
read, by construction. The only two exceptions are `ai_fields` and relic
`fields`/`saved`, which addendum rules 4 and 6 name as pass-through: the mod
itself filters them to primitive instance fields before they ever reach the
coach, and the converter on the other end reads only named, reviewed keys
out of them.

See docs/superpowers/specs/2026-09-24-snapshot-schemas.md section 1 for the
accepted shape, and
.superpowers/sdd/2026-09-24-stage0-1a-toolchain-mod-capture/task-11-addendum.md
for the exact per-field adapter rules this module follows.
"""

from __future__ import annotations

from collections import Counter, defaultdict

SCHEMA = "sts2-live-snapshot/1"

_COMBAT_ROOM_TYPES = frozenset({"monster", "elite", "boss"})
_RECOGNIZED_STATE_TYPES = _COMBAT_ROOM_TYPES | {"hand_select"}

_UNPLAYABLE_REASON = "Unplayable"

# The game's MonsterMoves.Intents.IntentType (decompiled IntentType.cs), all 15 values,
# spelled the live snapshot's way (task-11-fix-1.md C1). IntentType.ToString() is
# CamelCase, so a bare .upper() mangles every multi-word member (DeathBlow, StatusCard,
# DebuffStrong, CardDebuff) -- this table is the fix. Cross-checked against the
# converter's own accepted vocabulary (sts2_gym/live/ids.py INTENT_TYPES) in
# tests/test_snapshot.py.
_INTENT_TYPE_NAMES: dict[str, str] = {
    "Attack": "ATTACK",
    "DeathBlow": "DEATH_BLOW",
    "Defend": "DEFEND",
    "Buff": "BUFF",
    "Summon": "SUMMON",
    "Heal": "HEAL",
    "Debuff": "DEBUFF",
    "DebuffStrong": "DEBUFF_STRONG",
    "CardDebuff": "CARD_DEBUFF",
    "StatusCard": "STATUS_CARD",
    "Sleep": "SLEEP",
    "Stun": "STUN",
    "Hidden": "HIDDEN",
    "Escape": "ESCAPE",
    "Unknown": "UNKNOWN",
}
# "An intent that is an attack -- type "Attack", or "DeathBlow"" (raw-full.md).
_ATTACK_RAW_INTENT_TYPES = frozenset({"Attack", "DeathBlow"})

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
    """A required field was missing, unreadable or invalid, at the given dotted JSON path."""

    def __init__(self, path: str, detail: str | None = None) -> None:
        self.path = path
        message = f"missing or invalid required field: {path}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)


# ---------------------------------------------------------------------------
# Typed, path-carrying field reads (task-11-fix-1.md M3). Every field this
# module treats as required goes through one of these -- never a bare
# container[key] or a silent .get(..., default) for a field the mod always
# sends. A malformed field (wrong type, or an index used to build a later
# path) raises SnapshotError with a real path instead of a bare exception.
# ---------------------------------------------------------------------------


def _require(container: dict, key: str, path: str):
    """The value at container[key], or raise SnapshotError(path) if it's absent or null."""
    if not isinstance(container, dict) or container.get(key) is None:
        raise SnapshotError(path)
    return container[key]


def _require_typed(container: dict, key: str, path: str, checker, type_name: str):
    value = _require(container, key, path)
    if not checker(value):
        raise SnapshotError(path, f"expected {type_name}, got {type(value).__name__}")
    return value


def _require_str(container: dict, key: str, path: str) -> str:
    return _require_typed(container, key, path, lambda v: isinstance(v, str), "a string")


def _require_int(container: dict, key: str, path: str) -> int:
    return _require_typed(
        container,
        key,
        path,
        lambda v: isinstance(v, int) and not isinstance(v, bool),
        "an int",
    )


def _require_bool(container: dict, key: str, path: str) -> bool:
    return _require_typed(container, key, path, lambda v: isinstance(v, bool), "a bool")


def _require_dict(container: dict, key: str, path: str) -> dict:
    return _require_typed(container, key, path, lambda v: isinstance(v, dict), "an object")


def _require_list(container: dict, key: str, path: str) -> list:
    return _require_typed(container, key, path, lambda v: isinstance(v, list), "an array")


# ---------------------------------------------------------------------------
# "Unreadable" fields (task-11-fix-1.md I1). The mod sends an explicit null
# for card fields, relic fields, ai_fields and attack-intent numbers when it
# could not read them, with the reason recorded in the state's own
# `warnings` list under a fixed context string (e.g. "card.fields"; see
# McpMod.StateExtras.cs's guard sites, mirrored in Task 5's report). That
# null means UNREADABLE, not "empty" -- it must never quietly become {} or a
# null number. This is a different case from a field the schema documents as
# legitimately nullable by design (card uid/cost's "X", relic counter/saved,
# enemy move_id/state_log): those still use plain .get().
# ---------------------------------------------------------------------------


def _matching_warning(warnings: list[str], context: str) -> str | None:
    prefix = f"{context}:"
    for entry in warnings:
        if isinstance(entry, str) and entry.startswith(prefix):
            return entry
    return None


def _require_readable(container: dict, key: str, path: str, *, warnings: list[str], context: str):
    value = container.get(key) if isinstance(container, dict) else None
    if value is None:
        warning = _matching_warning(warnings, context)
        raise SnapshotError(path, warning or f"the mod could not read {context}")
    return value


def _require_readable_dict(
    container: dict, key: str, path: str, *, warnings: list[str], context: str
) -> dict:
    value = _require_readable(container, key, path, warnings=warnings, context=context)
    if not isinstance(value, dict):
        raise SnapshotError(path, f"expected an object, got {type(value).__name__}")
    return value


def _require_readable_int(
    container: dict, key: str, path: str, *, warnings: list[str], context: str
) -> int:
    value = _require_readable(container, key, path, warnings=warnings, context=context)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SnapshotError(path, f"expected an int, got {type(value).__name__}")
    return value


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


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
    return {k: v for k, v in fields.items() if isinstance(v, (bool, int, str))}


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


def _build_cost(card: dict, *, path: str) -> int | None:
    """The mod's cost is a string: "X" becomes null.

    Unplayable becomes null based on the card's own unplayable signal, not
    the sign of the number (task-11-fix-1.md reviewer note): a relative
    cost override CAN legitimately push a playable card below 0 (Kingly
    Kick, per Task 5's report), so a negative cost alone is not evidence of
    unplayability. In the hand, `unplayable_reason == "Unplayable"` is that
    signal. Pile Card Objects and hand_select candidates carry no such
    signal at all (confirmed against McpMod.StateBuilder.cs's
    BuildPileCardList, which never sets can_play/unplayable_reason/keywords)
    -- for those this only ever nulls "X", and passes the raw parsed number
    through otherwise, negative or not.
    """
    raw_cost = _require_str(card, "cost", f"{path}.cost")
    if raw_cost == "X":
        return None
    try:
        value = int(raw_cost)
    except ValueError as exc:
        raise SnapshotError(f"{path}.cost", f"unparseable cost {raw_cost!r}") from exc
    if card.get("unplayable_reason") == _UNPLAYABLE_REASON:
        return None
    return value


def _build_card_fields(fields: dict) -> dict:
    """Only cost_this_turn / cost_for_combat, each only when the mod set it."""
    return {key: fields[key] for key in _COST_FIELD_KEYS if key in fields}


def _build_card(card: dict, *, path: str, warnings: list[str]) -> dict:
    enchantment = card.get("enchantment")
    fields = _require_readable_dict(
        card, "fields", f"{path}.fields", warnings=warnings, context="card.fields"
    )
    return {
        "id": _require_str(card, "id", f"{path}.id"),
        "upgraded": _require_bool(card, "is_upgraded", f"{path}.is_upgraded"),
        "uid": card.get("uid"),
        "cost": _build_cost(card, path=path),
        "enchantment": enchantment.get("id") if enchantment else None,
        "enchant_amount": enchantment.get("amount") if enchantment else None,
        "fields": _build_card_fields(fields),
    }


def _draw_pile_sort_key(card: dict):
    """A total order over the WHOLE output card (task-11-fix-1.md I2).

    Sorting on `id`/`upgraded` alone lets two cards that differ only in
    enchantment or cost overrides tie, and a tie-break that fell through to
    input order would leak a sliver of the true draw order through which
    of two otherwise-identical cards happened to sort first. `uid` is safe
    as the final tiebreaker even though it is per-instance: it is assigned
    once per card in pile-serialization order (hand, sorted draw, discard,
    exhaust), never in true draw order (Task 5's report), so it carries no
    positional information -- it only makes the ordering deterministic.
    """
    fields = card["fields"]
    return (
        card["id"],
        card["upgraded"],
        card["enchantment"] or "",
        card["enchant_amount"] or 0,
        card["cost"] if card["cost"] is not None else -1,
        sorted(fields.items()),
        card["uid"] or "",
    )


def _build_hand(player: dict, *, warnings: list[str]) -> list[dict]:
    hand = _require_list(player, "hand", "player.hand")
    indexed = [
        (_require_int(c, "index", f"player.hand[{i}].index"), c) for i, c in enumerate(hand)
    ]
    indexed.sort(key=lambda pair: pair[0])
    return [_build_card(c, path=f"hand[{idx}]", warnings=warnings) for idx, c in indexed]


def _build_draw_pile(player: dict, *, warnings: list[str]) -> list[dict]:
    """draw_pile is an UNORDERED multiset, sorted on a total key for stability.

    Built only from player.draw_pile -- player.draw_pile_ordered (the true
    draw order) is hidden information and is never read here.
    """
    draw_pile = _require_list(player, "draw_pile", "player.draw_pile")
    cards = [
        _build_card(c, path=f"draw_pile[{i}]", warnings=warnings)
        for i, c in enumerate(draw_pile)
    ]
    return sorted(cards, key=_draw_pile_sort_key)


def _build_ordered_pile(
    player: dict, *, unordered_key: str, ordered_key: str, path: str, warnings: list[str]
) -> list[dict]:
    """discard_pile / exhaust_pile: true pile order comes from *_ordered when present.

    *_ordered entries are the minimal {id, is_upgraded, uid} shape; each is
    matched by uid to its full Pile Card Object in the unordered list to
    recover cost/enchantment/fields. Falls back to the unordered list's own
    order if *_ordered is absent from the payload.

    *_ordered must be a permutation of the unordered pile: same length and
    the same multiset of uids (task-11-fix-1.md I1), checked with a Counter
    rather than a set so cards sharing a uid -- or both missing one --
    cannot silently collapse into a single matched entry.
    """
    unordered = _require_list(player, unordered_key, f"player.{unordered_key}")
    for i, card in enumerate(unordered):
        if not isinstance(card, dict):
            raise SnapshotError(
                f"player.{unordered_key}[{i}]", f"expected an object, got {type(card).__name__}"
            )

    ordered = player.get(ordered_key)
    if ordered is not None and not isinstance(ordered, list):
        raise SnapshotError(
            f"player.{ordered_key}", f"expected an array, got {type(ordered).__name__}"
        )
    if not ordered:
        return [
            _build_card(c, path=f"{path}[{i}]", warnings=warnings)
            for i, c in enumerate(unordered)
        ]

    for i, entry in enumerate(ordered):
        if not isinstance(entry, dict):
            raise SnapshotError(
                f"player.{ordered_key}[{i}]", f"expected an object, got {type(entry).__name__}"
            )

    if len(ordered) != len(unordered):
        raise SnapshotError(
            f"player.{ordered_key}",
            f"length {len(ordered)} does not match player.{unordered_key} "
            f"length {len(unordered)} (not a permutation)",
        )

    unordered_uids = Counter(c.get("uid") for c in unordered)
    ordered_uids = Counter(e.get("uid") for e in ordered)
    if unordered_uids != ordered_uids:
        raise SnapshotError(
            f"player.{ordered_key}",
            f"is not a permutation of player.{unordered_key} (uid multisets differ)",
        )

    pool: dict[object, list[dict]] = defaultdict(list)
    for card in unordered:
        pool[card.get("uid")].append(card)

    result = []
    for i, entry in enumerate(ordered):
        card = pool[entry.get("uid")].pop(0)
        result.append(_build_card(card, path=f"{path}[{i}]", warnings=warnings))
    return result


# ---------------------------------------------------------------------------
# Powers, relics, orbs, potions
# ---------------------------------------------------------------------------


def _build_power(power: dict, *, path: str) -> dict:
    return {
        "id": _require_str(power, "id", f"{path}.id"),
        "amount": _require_int(power, "amount", f"{path}.amount"),
    }


def _build_powers(powers: list, *, path: str) -> list[dict]:
    return [_build_power(p, path=f"{path}[{i}]") for i, p in enumerate(powers)]


def _build_relic(relic: dict, *, path: str, warnings: list[str]) -> dict:
    fields = _require_readable_dict(
        relic, "fields", f"{path}.fields", warnings=warnings, context="relic.fields"
    )
    # `saved` is null both when the relic legitimately saves nothing and on
    # read failure (raw-full.md is explicit both ways); task-11-fix-1.md I1
    # scopes the unreadable-means-raise treatment to card/relic `fields`,
    # `ai_fields` and attack numbers only, so `saved` stays a plain optional
    # read rather than another _require_readable_dict.
    saved = relic.get("saved")
    merged = {**_primitive_fields(fields), **_primitive_fields(saved)}
    return {
        "id": _require_str(relic, "id", f"{path}.id"),
        "counter": relic.get("counter"),
        "fields": merged,
    }


def _build_orbs(player: dict) -> dict:
    # Defect-only; the mod omits orbs/orb_slots entirely rather than sending
    # them null when orb capacity is 0, so this stays a plain default (not
    # one of I1's "always sent" fields).
    orbs = player.get("orbs") or []
    return {
        "slots": player.get("orb_slots", 0),
        "list": [
            {
                "id": _require_str(orb, "id", f"player.orbs[{i}].id"),
                "passive": orb.get("passive_val"),
                "evoke": orb.get("evoke_val"),
            }
            for i, orb in enumerate(orbs)
        ],
    }


def _build_potions(potions: list, *, max_potion_slots: int) -> list[dict]:
    slots: list[dict] = [{"slot": i, "id": None} for i in range(max_potion_slots)]
    seen_slots: set[int] = set()
    for i, potion in enumerate(potions):
        slot = _require_int(potion, "slot", f"player.potions[{i}].slot")
        if not (0 <= slot < max_potion_slots):
            raise SnapshotError(
                f"player.potions[{i}].slot",
                f"slot {slot} out of range for max_potion_slots={max_potion_slots}",
            )
        if slot in seen_slots:
            raise SnapshotError(f"player.potions[{i}].slot", f"duplicate potion in slot {slot}")
        seen_slots.add(slot)
        slots[slot]["id"] = _require_str(potion, "id", f"player.potions[{i}].id")
    return slots


def _build_player(player: dict, *, warnings: list[str]) -> dict:
    pets = player.get("pets")
    if pets:
        raise SnapshotError("player.pets", "pets are not modelled")

    max_potion_slots = _require_int(player, "max_potion_slots", "player.max_potion_slots")
    status = _require_list(player, "status", "player.status")
    relics = _require_list(player, "relics", "player.relics")
    potions = _require_list(player, "potions", "player.potions")

    return {
        "hp": _require_int(player, "hp", "player.hp"),
        "max_hp": _require_int(player, "max_hp", "player.max_hp"),
        "block": _require_int(player, "block", "player.block"),
        "energy": _require_int(player, "energy", "player.energy"),
        "max_energy": _require_int(player, "max_energy", "player.max_energy"),
        "gold": _require_int(player, "gold", "player.gold"),
        "stars": player.get("stars", 0),
        "osty": None,  # Necrobinder pets are out of scope; see task-11-report.md.
        "orbs": _build_orbs(player),
        "powers": _build_powers(status, path="player.status"),
        "relics": [
            _build_relic(r, path=f"player.relics[{i}]", warnings=warnings)
            for i, r in enumerate(relics)
        ],
        "potions": _build_potions(potions, max_potion_slots=max_potion_slots),
        "max_potion_slots": max_potion_slots,
    }


# ---------------------------------------------------------------------------
# Enemies
# ---------------------------------------------------------------------------


def _build_intent(intent: dict, *, path: str, warnings: list[str]) -> dict:
    raw_type = _require_str(intent, "type", f"{path}.type")
    mapped_type = _INTENT_TYPE_NAMES.get(raw_type)
    if mapped_type is None:
        raise SnapshotError(f"{path}.type", f"unmapped intent type {raw_type!r}")

    if raw_type not in _ATTACK_RAW_INTENT_TYPES:
        return {"type": mapped_type, "damage": None, "base_damage": None, "hits": None}

    return {
        "type": mapped_type,
        "damage": _require_readable_int(
            intent, "damage", f"{path}.damage", warnings=warnings, context="enemy.intent.damage"
        ),
        "base_damage": _require_readable_int(
            intent,
            "base_damage",
            f"{path}.base_damage",
            warnings=warnings,
            context="enemy.intent.base_damage",
        ),
        "hits": _require_readable_int(
            intent, "hits", f"{path}.hits", warnings=warnings, context="enemy.intent.hits"
        ),
    }


def _build_enemy(enemy: dict, *, path: str, warnings: list[str]) -> dict | None:
    """None when the enemy should be dropped (dead, no revive)."""
    alive = _require_bool(enemy, "alive", f"{path}.alive")
    if not alive and not enemy.get("revive"):
        return None

    entity_id = _require_str(enemy, "entity_id", f"{path}.entity_id")
    ai_fields = _require_readable_dict(
        enemy, "ai_fields", f"{path}.ai_fields", warnings=warnings, context="enemy.ai_fields"
    )
    status = _require_list(enemy, "status", f"{path}.status")
    intents = _require_list(enemy, "intents", f"{path}.intents")
    return {
        "entity_id": entity_id,
        "id": _strip_entity_suffix(entity_id),
        "slot": enemy.get("slot"),
        "hp": _require_int(enemy, "hp", f"{path}.hp"),
        "max_hp": _require_int(enemy, "max_hp", f"{path}.max_hp"),
        "block": _require_int(enemy, "block", f"{path}.block"),
        "powers": _build_powers(status, path=f"{path}.status"),
        "intents": [
            _build_intent(intent, path=f"{path}.intents[{i}]", warnings=warnings)
            for i, intent in enumerate(intents)
        ],
        "move_id": enemy.get("move_id"),
        "state_log": enemy.get("state_log"),
        "ai_fields": _primitive_fields(ai_fields),
    }


def _build_enemies(battle: dict, *, warnings: list[str]) -> list[dict]:
    enemies = _require_list(battle, "enemies", "battle.enemies")
    built = (
        _build_enemy(e, path=f"enemies[{i}]", warnings=warnings) for i, e in enumerate(enemies)
    )
    return [e for e in built if e is not None]


def _build_history(battle: dict) -> dict:
    history = battle.get("history") or {}
    return {
        key: history[key]
        for key in _HISTORY_KEYS
        if key in history and history[key] is not None
    }


# ---------------------------------------------------------------------------
# Room type / state type
# ---------------------------------------------------------------------------


def _resolve_room_type(state: dict, room_type: str | None) -> str:
    """Validate state_type and pick the room type.

    A combat state's own state_type always wins: an explicit `room_type`
    that disagrees with it is a caller bug, not an override
    (task-11-fix-1.md M4). Only for `hand_select`, whose own state_type is
    not a room type, does the caller-supplied `room_type` fill the gap.
    """
    state_type = _require_str(state, "state_type", "state_type")
    if state_type not in _RECOGNIZED_STATE_TYPES:
        raise SnapshotError("state_type", f"unrecognized state_type {state_type!r}")

    if state_type in _COMBAT_ROOM_TYPES:
        if room_type is not None and room_type != state_type:
            raise SnapshotError(
                "room_type", f"got {room_type!r}, but state_type is {state_type!r}"
            )
        return state_type

    if room_type not in _COMBAT_ROOM_TYPES:
        raise SnapshotError("room_type", f"required for hand_select, got {room_type!r}")
    return room_type


# ---------------------------------------------------------------------------
# Top level
# ---------------------------------------------------------------------------


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
    state it defaults to `state["state_type"]` and must not disagree with it.

    `known_top` / `known_bottom` are the cards the player knowingly placed on
    top of / bottom of the draw pile (mod Pile Card Object shape), supplied
    by the caller -- the mod does not report this itself.

    Raises SnapshotError, naming the JSON path, for any required field that
    is missing, unreadable (an explicit mod null) or the wrong type.

    A `hand_select` state always raises: until the mod reports a selection's
    source card and bounds (a later task), this builder cannot fill schema
    section 1.1's `pending_selection` without inventing data
    (task-11-fix-1.md I3 / controller ruling).
    """
    run = _require_dict(state, "run", "run")
    player = _require_dict(state, "player", "player")
    battle = _require_dict(state, "battle", "battle")
    warnings = state.get("warnings") or []

    resolved_room_type = _resolve_room_type(state, room_type)

    if state.get("state_type") == "hand_select":
        raise SnapshotError(
            "hand_select", "the mod does not report this selection's source card and bounds yet"
        )

    character = _require_str(player, "character", "player.character")

    enemies = _build_enemies(battle, warnings=warnings)
    if not enemies:
        raise SnapshotError("enemies", "no alive or reviving enemy in this combat state")

    return {
        "schema": SCHEMA,
        "game_version": game_version,
        "character": _strip_category_prefix(character),
        "ascension": _require_int(run, "ascension", "run.ascension"),
        "act": _require_int(run, "act", "run.act"),
        "floor": _require_int(run, "floor", "run.floor"),
        "room_type": resolved_room_type,
        "round": _require_int(battle, "round", "battle.round"),
        "player": _build_player(player, warnings=warnings),
        "hand": _build_hand(player, warnings=warnings),
        "draw_pile": _build_draw_pile(player, warnings=warnings),
        "discard_pile": _build_ordered_pile(
            player,
            unordered_key="discard_pile",
            ordered_key="discard_pile_ordered",
            path="discard_pile",
            warnings=warnings,
        ),
        "exhaust_pile": _build_ordered_pile(
            player,
            unordered_key="exhaust_pile",
            ordered_key="exhaust_pile_ordered",
            path="exhaust_pile",
            warnings=warnings,
        ),
        "known_draw_top": [
            _build_card(c, path=f"known_draw_top[{i}]", warnings=warnings)
            for i, c in enumerate(known_top or [])
        ],
        "known_draw_bottom": [
            _build_card(c, path=f"known_draw_bottom[{i}]", warnings=warnings)
            for i, c in enumerate(known_bottom or [])
        ],
        "enemies": enemies,
        "history": _build_history(battle),
        "pending_selection": None,
    }
