# Snapshot Schemas

- **Date:** 2026-09-24
- **Status:** Binding interface between workstreams. Change it only with a ledger ruling.
- **Parent spec:** `2026-09-23-spire-coach-design.md`, sections 6.1–6.2

Two documents carry a combat position from the game into the simulator:

```
mod JSON ──(coach: coach/snapshot.py, visible-field allowlist)──▶ live snapshot  (game terms)
live snapshot ──(simulator fork: sts2_gym/live/convert.py)──▶ simulator snapshot (emulator ints)
simulator snapshot ──(C# Sts2_LoadCombatSnapshot)──▶ native combat handle
```

The live snapshot is the coach's contract. The simulator snapshot is the emulator's contract. All emulator-specific meaning lives in the second conversion step: ids, BuffIds, relic counter semantics and AI fields.

---

## 1. Live snapshot: `sts2-live-snapshot/1`

JSON. Strings use the game's `ModelId.Entry` form in UPPER_SNAKE_CASE with no category prefix, e.g. `STRIKE_IRONCLAD`, `BURNING_BLOOD`, `STRENGTH_POWER`, `JAW_WORM`. Only information visible to the player may appear. Any mod field that reveals hidden state (true draw order, RNG, pools, upcoming encounters) must never be copied in.

```jsonc
{
  "schema": "sts2-live-snapshot/1",
  "game_version": "v0.107.1",          // from release_info.json via the coach
  "character": "IRONCLAD",
  "ascension": 0,
  "act": 1,
  "floor": 3,
  "room_type": "monster",               // monster | elite | boss
  "round": 1,                           // game round number, 1-based
  "player": {
    "hp": 70, "max_hp": 80, "block": 0,
    "energy": 3, "max_energy": 3,       // as displayed
    "gold": 99, "stars": 0,
    "osty": null,                       // {"hp": int, "max_hp": int} for Necrobinder
    "orbs": {"slots": 0, "list": []},   // list items: {"id": "LIGHTNING_ORB", "passive": int, "evoke": int}
    "powers": [{"id": "STRENGTH_POWER", "amount": 2}],   // real stack counts
    "relics": [{"id": "BURNING_BLOOD", "counter": null, "fields": {}}], // counter as shown by the game (null if none); fields = primitive instance fields of the relic object (flags like "used this combat")
    "potions": [{"slot": 0, "id": "FIRE_POTION"}, {"slot": 1, "id": null}],
    "max_potion_slots": 3
  },
  "hand": [Card],                       // in hand order, left to right
  "draw_pile": [Card],                  // UNORDERED multiset of the WHOLE draw pile, including any known_draw_* cards (sorted by id for stability)
  "discard_pile": [Card],               // in pile order
  "exhaust_pile": [Card],
  "known_draw_top": [Card],             // cards the player knowingly placed on top, top first; default []
  "known_draw_bottom": [Card],          // bottom-most last; default []
  "enemies": [{
    "entity_id": "JAW_WORM_0",          // stable per combat (mod guarantees stability across deaths)
    "id": "JAW_WORM",
    "slot": 0,                          // left-to-right position among all enemy slots
    "hp": 40, "max_hp": 44, "block": 0,
    "powers": [{"id": "RITUAL_POWER", "amount": 3}],
    "intents": [{"type": "ATTACK", "damage": 11, "base_damage": 11, "hits": 1}],  // type from the game's IntentType; damage = displayed per hit, base_damage = unmodified per hit; null if n/a
    "move_id": "CHOMP",                 // current MoveState.StateId, null if unknown
    "state_log": ["CHOMP"],             // MonsterMoveStateMachine.StateLog ids, oldest first (includes branch states)
    "ai_fields": {}                     // primitive instance fields of the concrete monster class, by field name
  }],
  "history": {                          // ONLY these 7 keys are accepted (the converter rejects others); a missing key = unknown
    "cards_played_this_turn": 0,
    "cards_played_this_combat": 0,
    "cards_drawn_this_combat": 5,
    "cards_generated_this_combat": 0,
    "lightning_orbs_channeled_this_combat": 0,
    "attacks_played_this_turn": 0,
    "skills_played_this_turn": 0
  },
  "pending_selection": null             // see 1.1
}
```

`Card`:

```jsonc
{
  "id": "STRIKE_IRONCLAD",
  "upgraded": false,
  "uid": "c17",                 // mod card uid, stable while the card exists; null if unknown
  "cost": 1,                    // current displayed cost, null for unplayable/X
  "enchantment": null,          // game enchantment id string, e.g. "SHARP"
  "enchant_amount": null,
  "fields": {}                  // ONLY "cost_this_turn" / "cost_for_combat" (int, ABSOLUTE game energy cost) are understood; other keys produce a converter warning
}
```

Adapter rule for the coach (`coach/snapshot.py`): translate whatever raw per-card cost-override fields the mod reports into these two keys, and pass through only the 7 `history` keys above, dropping any other mod history key (e.g. `powers_played_this_turn`).

### 1.1 Pending in-combat selection

```jsonc
{
  "kind": "HAND",               // "HAND": pick from the hand (mod hand_select)
                                // "GRID": pick from a pile (mod card_select grid)
                                // "CHOOSE": pick one of freshly generated cards (mod choose-a-card)
  "pile": null,                 // GRID only: "DRAW" | "DISCARD" | "EXHAUST"; null otherwise or when unknown
  "prompt_key": null,           // the game's localization KEY of the prompt (locale-independent); null if unknown
  "source_card": "BURNING_PACT",// id of the card whose effect opened the selection; null if not a card or unknown
  "source_potion": null,        // id of the potion that opened it, else null
  "source_relic": null,         // id of the relic that opened it, else null
  "candidates": [Card],         // selectable cards in on-screen order (generated cards for CHOOSE)
  "min": 1, "max": 1,
  "can_skip": false,            // the screen offers a skip / confirm-with-none
  "selected_uids": []           // already toggled, if the mod reports it
}
```

Amended 2026-09-24 (ledger ruling, stage0 Task 11 findings). The mod reports in-combat hand, grid and choose-a-card overlays with the battle block, the selection's source, min/max and prompt key (mod Task 6c), and the coach's builder fills this object from them (coach Task 11b). `kind` is the SCREEN SHAPE, not a game enum: the game has no selection-kind enum. The converter derives the emulator's `CardSelectionKind` from `source_*` plus `kind`/`pile`, and warns when it cannot.

---

## 2. Simulator snapshot: `sts2-emu-snapshot/1`

JSON consumed by the native export `Sts2_LoadCombatSnapshot` and emitted by `Sts2_GetStateJson`. Every id is the emulator's `int`: card `DefId`, relic `DefId`, potion id, `BuffId` enum value, enemy `DefId`, `IntentType`, `Enchantment`, `CardSelectionKind`, orb type. Field names are snake_case versions of the C# fields they fill (`CombatState`, `EnemyState`, `CardInstance`). Omitted optional fields take the C# default.

```jsonc
{
  "schema": "sts2-emu-snapshot/1",
  "ascension": 0,
  "encounter_id": null,                 // informational
  "is_elite_combat": false,
  "is_elite_room": false,
  "fur_coat_bites": false,
  "turn": 0,                            // emulator Turn (0-based)
  "player": {
    "hp": 70, "max_hp": 80, "block": 0,
    "energy": 3,
    "max_energy": 3,                    // base 3 + flat relic bonuses (NOT the live display)
    "gold": 99, "stars": 0,
    "osty_hp": 0, "osty_max_hp": 0,
    "orb_capacity": 0, "base_orb_slots": 0,
    "orbs": [{"type": 0, "evoke": 0, "passive": 0}],
    "buffs": [{"id": 12, "amount": 2}],
    "debuffs_at_round_start": null      // null = derive with BuffSystem.DurationDebuffSnapshot
  },
  "relics": [{"id": 3, "counter": 0}],  // counters already in emulator meaning
  "potions": {"max_slots": 3, "slots": [0, 0, 0, 0, 0, 0]},   // 6 wide, 0 = empty
  "piles": {
    "hand": [EmuCard],
    "discard": [EmuCard],
    "exhaust": [EmuCard],
    "draw": {"known_top": [EmuCard], "known_bottom": [EmuCard], "unordered": [EmuCard]}
  },
  "incoming": {"return_to_hand": [EmuCard], "copies_to_hand": [EmuCard]},
  "enemies": [{
    "def_id": 17, "slot": 0,
    "hp": 40, "max_hp": 44, "block": 0,
    "buffs": [{"id": 30, "amount": 3}],
    "intent": {"type": 0, "magnitude": 11, "hits": 1, "carries_damage": false},  // BASE per-hit values
    "ai": {
      "move_index": 1, "last_move": 0, "last_move_repeats": 0, "once_only_move_used": false,
      "move_history": [0], "starts_on_branch": false, "rolls_next_move": false,
      "last_branch": -1, "repeat_streak": 0, "branch_cooldown": 0, "starter_move": 0,
      "off_balance": false, "nemesis_intangible_on": false, "escaped": false,
      "last_spawned_def_id": 0, "stolen_gold": 0, "heist_gold": 0,
      "powered_hits_this_turn": 0, "stolen_card": null
    }
  }],
  "counters": {                         // every CombatState per-turn/per-combat counter by snake_case name; omitted = 0/false
    "whispering_earring_spent": true
  },
  "pending_selection": null             // {"kind": int, "candidates": [int], "source_card_def_id": int, "amount": int,
                                        //  "skippable": bool, "generated_candidates": [int], "generated_upgraded": bool,
                                        //  "generated_retained": bool, "after_selection_to_hand": [EmuCard],
                                        //  "end_turn_awaiting_selection": bool}
}
```

`EmuCard`: `{"def_id": int, "upgraded": bool}` plus optional `CardInstance` fields in snake_case: `enchantment`, `enchant_amount`, `cost_bump`, `cost_for_combat` (null = unset), `cost_this_turn` (null = unset), `free_this_turn`, `free_until_played`, `retain_this_turn`, `retain_for_combat`, `sly_this_turn`, `sly_for_combat`, `ethereal_for_combat`, `played_this_turn`, `bound`, `dampened_upgrade`, `enchant_spent`, `replay_count`, `bonus_damage`, `bonus_block`, `tinker_type`, `tinker_rider`.

### 2.1 Round-trip rule

`GetStateJson(LoadCombatSnapshot(doc))` must equal `doc` after normalization:
- Omitted optional fields are filled with defaults.
- `piles.draw.unordered` is compared as a multiset.
- `debuffs_at_round_start: null` becomes its derived value.

### 2.2 Loading rules

- The loader skips the start-of-combat pipeline: no roster creation, no intent roll, no shuffle, no opening draw, and no relic combat-start or turn-start hooks.
- Buffs are added directly, never through `BuffSystem.Apply`.
- Streams are seeded from the load seed. `piles.draw.unordered` is shuffled with that seed and placed between `known_top` and `known_bottom`, with `KnownTopCount`/`KnownBottomCount` set accordingly.
- Unknown ids are an error, reported with a message naming the field. The loader never silently drops them.
