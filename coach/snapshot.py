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
out of them. Power `fields` are not an exception: only the keys the schema
names for each power are copied.

See docs/superpowers/specs/2026-09-24-snapshot-schemas.md section 1 for the
accepted shape, and
.superpowers/sdd/2026-09-24-stage0-1a-toolchain-mod-capture/task-11-addendum.md
for the exact per-field adapter rules this module follows.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

SCHEMA = "sts2-live-snapshot/1"

_COMBAT_ROOM_TYPES = frozenset({"monster", "elite", "boss"})
# hand_select and an in-combat card_select (grid or choose-a-card) both carry
# a `selection` object and neither's own state_type is a room type (task-11b).
_SELECTION_STATE_TYPES = frozenset({"hand_select", "card_select"})
_RECOGNIZED_STATE_TYPES = _COMBAT_ROOM_TYPES | _SELECTION_STATE_TYPES

# player.character_id is locale-independent (mod Task 6c). An older mod
# without it only reports the English-language title, so this maps that
# title back to the id it would have sent (task-11b requirement 1).
_CHARACTER_TITLE_IDS: dict[str, str] = {
    "The Ironclad": "IRONCLAD",
    "The Silent": "SILENT",
    "The Defect": "DEFECT",
    "The Necrobinder": "NECROBINDER",
    "The Regent": "REGENT",
}
_CHARACTER_IDS = frozenset(_CHARACTER_TITLE_IDS.values())

# selection.source.type values the schema has a field for; a "power" or
# "unknown" source has none, so all three source_* fields stay null instead
# (task-11b requirement 3).
_SELECTION_SOURCE_FIELDS: dict[str, str] = {
    "card": "source_card",
    "potion": "source_potion",
    "relic": "source_relic",
}

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

# Power `fields` (schema section 1, amended 2026-09-27): the state a power
# keeps beside its amount that the converter reads, by power. The mod reports
# them under the same names (STS2MCP docs/raw-full.md, power `fields`); any
# other key, and any key on another power, is never copied.
_POWER_FIELD_KEYS: dict[str, tuple[str, ...]] = {
    "SKITTISH_POWER": ("has_gained_block_this_turn",),
    "SURROUNDED_POWER": ("facing",),
    "POSSESS_SPEED_POWER": ("stolen",),
    "POSSESS_STRENGTH_POWER": ("stolen",),
    "SWIPE_POWER": ("stolen_card", "stolen_card_upgraded"),
    "THIEVERY_POWER": ("stolen_gold",),
}
# SwipePower's hover tip is the card it stole (decompiled SwipePower
# `ExtraHoverTips`), which the mod reports as a keyword id "CARD.<ENTRY>", with
# a trailing "+" when upgraded. A mod without power fields still sends it.
_SWIPE_CARD_TIP = re.compile(r"CARD\.([A-Z0-9_]+)(\+?)")

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

    Unplayable becomes null based on the card's own `unplayable` signal, now
    present on every card object -- hand, pile, deck and selection-screen
    alike (mod Task 6c) -- not the sign of the number: a relative cost
    override can legitimately push a playable card below 0 (Kingly Kick, per
    Task 5's report), so a negative cost alone is not evidence of
    unplayability when the flag is there to ask instead (task-11b
    requirement 5).

    An older mod that predates the flag sends no `unplayable` key at all
    (never `null`; `null` there means "the flag itself is unreadable", not
    "absent", and is treated the same as `false` here since only `true` is
    ever unambiguous). Without it, this falls back to the previous
    approximation: the game's own "unplayable" cost convention is negative
    (a curse or status), so a negative cost becomes null too, and a
    non-negative one passes through unchanged.
    """
    raw_cost = _require_str(card, "cost", f"{path}.cost")
    if raw_cost == "X":
        return None
    try:
        value = int(raw_cost)
    except ValueError as exc:
        raise SnapshotError(f"{path}.cost", f"unparseable cost {raw_cost!r}") from exc

    unplayable = card.get("unplayable")
    if unplayable is not None:
        return None if unplayable else value
    return None if value < 0 else value


def _build_card_fields(fields: dict) -> dict:
    """Only cost_this_turn / cost_for_combat, each only when the mod set it."""
    return {key: fields[key] for key in _COST_FIELD_KEYS if key in fields}


def _build_card(card: dict, *, path: str, warnings: list[str]) -> dict:
    enchantment = card.get("enchantment")
    fields = _require_readable_dict(
        card, "fields", f"{path}.fields", warnings=warnings, context="card.fields"
    )
    built = {
        "id": _require_str(card, "id", f"{path}.id"),
        "upgraded": _require_bool(card, "is_upgraded", f"{path}.is_upgraded"),
        "uid": card.get("uid"),
        "cost": _build_cost(card, path=path),
        "enchantment": enchantment.get("id") if enchantment else None,
        "enchant_amount": enchantment.get("amount") if enchantment else None,
        "fields": _build_card_fields(fields),
    }
    # Optional, only when set (e.g. the Queen's "BOUND"): sts2_gym.live.convert.AFFLICTIONS.
    if isinstance(card.get("affliction"), str):
        built["affliction"] = card["affliction"]
    return built


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
    order if *_ordered is absent (None) from the payload.

    An explicitly empty *_ordered ([]) is NOT treated as absent (task-11b,
    carried over from Task 11's re-review): it still has to be a permutation
    of the unordered pile, so it raises via the length check below when the
    unordered pile is non-empty, exactly like any other length mismatch.

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
    if ordered is None:
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


def _build_power_fields(entry: str, power: dict) -> dict:
    """Copy the named `fields` of a power (`_POWER_FIELD_KEYS`); {} for any other.

    Optional: a mod that predates them, or could not read them (null), sends
    none, and the converter says what that leaves out. Swipe's card falls back
    to its hover tip.
    """
    raw = power.get("fields")
    raw = raw if isinstance(raw, dict) else {}
    fields = {
        key: raw[key]
        for key in _POWER_FIELD_KEYS.get(entry, ())
        if key in raw and (raw[key] is None or isinstance(raw[key], (bool, int, str)))
    }
    if entry == "SWIPE_POWER" and "stolen_card" not in fields:
        for tip in power.get("keywords") or []:
            tip_id = tip.get("id") if isinstance(tip, dict) else None
            match = _SWIPE_CARD_TIP.fullmatch(tip_id) if isinstance(tip_id, str) else None
            if match is not None:
                fields["stolen_card"] = match.group(1)
                fields["stolen_card_upgraded"] = match.group(2) == "+"
                break
    return fields


def _build_power(power: dict, *, path: str) -> dict:
    entry = _require_str(power, "id", f"{path}.id")
    built: dict = {
        "id": entry,
        "amount": _require_int(power, "amount", f"{path}.amount"),
    }
    fields = _build_power_fields(entry, power)
    if fields:
        built["fields"] = fields
    return built


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
# Character (task-11b requirement 1)
# ---------------------------------------------------------------------------


def _character_from_title(player: dict, *, warnings: list[str]) -> str:
    """Fallback for an older mod that has no `character_id`: map its
    English-language `character` title back to the id."""
    title = _require_str(player, "character", "player.character")
    mapped = _CHARACTER_TITLE_IDS.get(title)
    if mapped is None:
        raise SnapshotError("player.character", f"unrecognized character title {title!r}")
    warnings.append(
        f"player.character_id is missing (an older mod); mapped the English title "
        f"{title!r} to {mapped!r} instead"
    )
    return mapped


def _build_character(player: dict, *, warnings: list[str]) -> str:
    character_id = player.get("character_id")
    if character_id is None:
        return _character_from_title(player, warnings=warnings)
    if character_id not in _CHARACTER_IDS:
        raise SnapshotError("player.character_id", f"unrecognized character id {character_id!r}")
    return character_id


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


def _shows_heal(enemy: dict) -> bool:
    """Whether a corpse's displayed move is a Heal: `revive.in_turns` 1 in the mod's terms.

    A mod before 2026-09-27 sent no `revive` for an Illusion or Adaptable corpse (the
    Obscura's Parafright, the Test Subject), whose power forces that Heal move on death.
    """
    intents = enemy.get("intents")
    return isinstance(intents, list) and any(
        isinstance(intent, dict) and intent.get("type") == "Heal" for intent in intents
    )


def _build_enemy(enemy: dict, *, path: str, warnings: list[str]) -> dict | None:
    """None when the enemy should be dropped (dead, and not coming back)."""
    alive = _require_bool(enemy, "alive", f"{path}.alive")
    if not alive and not enemy.get("revive") and not _shows_heal(enemy):
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
    (task-11-fix-1.md M4). Only for `hand_select` and an in-combat
    `card_select`, whose own state_type is not a room type, does the
    caller-supplied `room_type` fill the gap -- from the battle block's own
    combat, i.e. the last combat state_type the caller saw (task-11b
    requirement 3; unchanged mechanism, now also covering card_select).

    A `card_select` that is not in combat has no business reaching this
    combat-only builder at all, so it raises here rather than silently being
    treated as if it were mid-fight.
    """
    state_type = _require_str(state, "state_type", "state_type")
    if state_type not in _RECOGNIZED_STATE_TYPES:
        raise SnapshotError("state_type", f"unrecognized state_type {state_type!r}")

    if state_type == "card_select" and state.get("in_combat") is not True:
        raise SnapshotError(
            "in_combat", f"card_select requires in_combat=true, got {state.get('in_combat')!r}"
        )

    if state_type in _COMBAT_ROOM_TYPES:
        if room_type is not None and room_type != state_type:
            raise SnapshotError(
                "room_type", f"got {room_type!r}, but state_type is {state_type!r}"
            )
        return state_type

    if room_type not in _COMBAT_ROOM_TYPES:
        raise SnapshotError("room_type", f"required for {state_type}, got {room_type!r}")
    return room_type


# ---------------------------------------------------------------------------
# Pending in-combat selection (schema section 1.1; task-11b requirements 3/4)
# ---------------------------------------------------------------------------


def _build_selection_source(
    source: dict, *, path: str, warnings: list[str]
) -> dict[str, str | None]:
    """Which of source_card/source_potion/source_relic gets source.id; the
    other two are null. A power or unknown source has no field for it at
    all, so all three are null, with a warning (requirement 3)."""
    result: dict[str, str | None] = {
        "source_card": None,
        "source_potion": None,
        "source_relic": None,
    }
    source_type = source.get("type")
    field = _SELECTION_SOURCE_FIELDS.get(source_type)
    if field is None:
        warnings.append(
            f"{path}.source.type is {source_type!r}: the live snapshot has no field for a "
            "power or unknown selection source, so source_card/source_potion/source_relic "
            "are all null"
        )
        return result
    result[field] = source.get("id")
    return result


def _build_played_card(
    player: dict, source: dict, *, mod_warnings: list[str], warnings: list[str]
) -> dict | None:
    """The full Card in player.play_pile matched by the source card's uid
    (controller ruling, schema section 1.1 `played_card`). Null, with a
    warning, when the source is a card but no match is found -- including
    when the mod does not report play_pile at all."""
    if source.get("type") != "card":
        return None
    source_uid = source.get("uid")
    play_pile = player.get("play_pile")
    if play_pile is not None and not isinstance(play_pile, list):
        raise SnapshotError(
            "player.play_pile", f"expected an array, got {type(play_pile).__name__}"
        )
    for i, card in enumerate(play_pile or []):
        if isinstance(card, dict) and card.get("uid") == source_uid:
            return _build_card(card, path=f"player.play_pile[{i}]", warnings=mod_warnings)
    warnings.append(
        "pending_selection.played_card: no player.play_pile card matches the selection's "
        "source card uid; played_card is null"
    )
    return None


def _build_pending_selection(
    state: dict, *, state_type: str, player: dict, mod_warnings: list[str], warnings: list[str]
) -> dict | None:
    """Schema section 1.1, built from the mod's `selection` object on the
    hand_select/card_select screen, or None with a warning when an older mod
    has no `selection` object at all (requirement 4: nothing invented)."""
    screen = _require_dict(state, state_type, state_type)
    selection = screen.get("selection")
    if not isinstance(selection, dict):
        warnings.append(
            f"{state_type} has no selection object (an older mod); pending_selection is null"
        )
        return None

    path = f"{state_type}.selection"
    source = _require_dict(selection, "source", f"{path}.source")
    source_fields = _build_selection_source(source, path=path, warnings=warnings)

    cards = _require_list(screen, "cards", f"{state_type}.cards")
    candidates = [
        _build_card(c, path=f"{state_type}.cards[{i}]", warnings=mod_warnings)
        for i, c in enumerate(cards)
    ]

    selected_uids = [
        entry.get("uid") for entry in (selection.get("selected") or []) if isinstance(entry, dict)
    ]

    return {
        "kind": _require_str(selection, "kind", f"{path}.kind"),
        "pile": selection.get("pile"),
        "prompt_key": selection.get("prompt_key"),
        "source_card": source_fields["source_card"],
        "source_potion": source_fields["source_potion"],
        "source_relic": source_fields["source_relic"],
        "played_card": _build_played_card(
            player, source, mod_warnings=mod_warnings, warnings=warnings
        ),
        "candidates": candidates,
        "min": _require_int(selection, "min", f"{path}.min"),
        "max": _require_int(selection, "max", f"{path}.max"),
        "can_skip": _require_bool(selection, "can_skip", f"{path}.can_skip"),
        "selected_uids": selected_uids,
    }


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
    warnings: list[str] | None = None,
) -> dict:
    """Build a `sts2-live-snapshot/1` dict from one mod combat state payload.

    `state` is the mod's JSON for a `monster`/`elite`/`boss` state, or a
    `hand_select` / in-combat `card_select` state (which still carries the
    full `battle` object). `room_type` must be supplied explicitly for those
    selection states, since their own `state_type` is not one of
    monster/elite/boss; for a combat state it defaults to
    `state["state_type"]` and must not disagree with it.

    `known_top` / `known_bottom` are the cards the player knowingly placed on
    top of / bottom of the draw pile (mod Pile Card Object shape), supplied
    by the caller -- the mod does not report this itself.

    `warnings`, if given, receives one readable sentence per approximation
    this builder makes (an older mod missing a field it now expects, a
    selection source the schema has no field for...). These never go inside
    the returned snapshot -- the converter rejects unknown keys -- so they
    are only ever reported through this sink (task-11b requirement 0).

    Raises SnapshotError, naming the JSON path, for any required field that
    is missing, unreadable (an explicit mod null) or the wrong type.
    """
    if warnings is None:
        warnings = []

    run = _require_dict(state, "run", "run")
    player = _require_dict(state, "player", "player")
    battle = _require_dict(state, "battle", "battle")
    mod_warnings = state.get("warnings") or []

    resolved_room_type = _resolve_room_type(state, room_type)
    state_type = state.get("state_type")

    character = _build_character(player, warnings=warnings)

    enemies = _build_enemies(battle, warnings=mod_warnings)
    if not enemies:
        raise SnapshotError("enemies", "no alive or reviving enemy in this combat state")

    if state_type in _SELECTION_STATE_TYPES:
        pending_selection = _build_pending_selection(
            state,
            state_type=state_type,
            player=player,
            mod_warnings=mod_warnings,
            warnings=warnings,
        )
    else:
        pending_selection = None

    return {
        "schema": SCHEMA,
        "game_version": game_version,
        "character": character,
        "ascension": _require_int(run, "ascension", "run.ascension"),
        "act": _require_int(run, "act", "run.act"),
        "floor": _require_int(run, "floor", "run.floor"),
        "room_type": resolved_room_type,
        "round": _require_int(battle, "round", "battle.round"),
        "player": _build_player(player, warnings=mod_warnings),
        "hand": _build_hand(player, warnings=mod_warnings),
        "draw_pile": _build_draw_pile(player, warnings=mod_warnings),
        "discard_pile": _build_ordered_pile(
            player,
            unordered_key="discard_pile",
            ordered_key="discard_pile_ordered",
            path="discard_pile",
            warnings=mod_warnings,
        ),
        "exhaust_pile": _build_ordered_pile(
            player,
            unordered_key="exhaust_pile",
            ordered_key="exhaust_pile_ordered",
            path="exhaust_pile",
            warnings=mod_warnings,
        ),
        "known_draw_top": [
            _build_card(c, path=f"known_draw_top[{i}]", warnings=mod_warnings)
            for i, c in enumerate(known_top or [])
        ],
        "known_draw_bottom": [
            _build_card(c, path=f"known_draw_bottom[{i}]", warnings=mod_warnings)
            for i, c in enumerate(known_bottom or [])
        ],
        "enemies": enemies,
        "history": _build_history(battle),
        "pending_selection": pending_selection,
    }
