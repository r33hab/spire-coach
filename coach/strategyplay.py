"""Map the strategy champion's best option (`coach.engine.EngineClient.strategy`) to a
concrete mod action, for the autopilot's non-combat screens.

Only the option kinds below are mapped with confidence: each one ties a training-design
option kind (`sts2_gym/live/strategy.py`'s `label()`, the closest thing to a published
schema available here) to a mod action confirmed from the STS2MCP README and
`McpMod.Actions.cs`'s own dispatch table. Every other kind, and any option this module
cannot place a concrete, in-range action for, makes `execute_best_option` return False,
so the caller (`coach.autopilot`) falls back to `coach.navigator.advance_one_step` --
the same "first non-skip option, then proceed" rule used when the champion has no
answer at all. Documented limit: between-fight play outside these kinds is that
generic fallback, not actually the champion's pick, until this table is widened
against a live run.

`card.take` maps to `select_card_reward` (2026-09-28: the user disabled the workshop
content mods -- Watcher, The Shape, Multiplayer Expanded -- in `settings.save`, the
ones that grow Ironclad's card pool past what the simulator, the converter and the
strategy champion were trained on). A card the training package's own name table still
cannot place arrives as an `unknown`-kind option upstream (`rl/run_train/features.py`'s
`_card_option`, on `UnknownIdError`) and is unscorable, so it sorts last and is never
`options[0]` unless every offered card is unrecognized -- this module never needs its
own extra guard for that case.

Shop buys and the removal (`shop.card`/`shop.relic`/`shop.potion`/`shop.remove`),
rest-site options (`rest.*`), event options (`event.option`) and the deck screens that
follow them (`select.upgrade`/`select.remove`/`select.transform`/...) are the model's
pick too: each `action` is the live screen's own index (`rl/run_train/features.py`
`_live_shop`/`_live_rest`/`_live_event`/`_live_card_select`), what `shop_purchase`,
`choose_rest_option`, `choose_event_option` and `select_card` take. Until 2026-10-05
they fell to the generic fallback, which never bought anything, rested or took an
event's first option whatever the model said, and upgraded or removed the first card.
An action the mod refuses (an error response) falls back too.

`neow.relic` and `select.offer` (bug 7/7: Neow's relic pick and every card_select
screen's own offered cards -- a rest site's Smith upgrade, a shop's removal, an event's
or Neow's transform bonus, all built uniformly as `select.offer` by
`rl/run_train/features.py`) both map like `card.take`: `option["action"]` is already
the screen's own index. Deliberately still unmapped: `sphere.cell` (the Crystal
Sphere) and `merchant.throw` (the Fake Merchant's foul-potion fight) -- neither kind's
own live option shape (as opposed to its state, which `coach.navigator`'s generic
fallback does handle) could be confirmed from the training source without a live run
to check it against, so getting either wrong risks a silently-accepted bad action
(`_post` does not check the mod's response) worse than leaving it to the fallback.
"""

from __future__ import annotations

import time

from coach import navigator
from coach.bridge.client import BridgeError, ModClient
from coach.engine import StrategyResult

_NODE_TYPE_NAMES = {
    1: "Monster",
    2: "Elite",
    3: "Rest Site",
    4: "Shop",
    5: "Treasure",
    6: "Boss",
    7: "Unknown",
    8: "Ancient",
}


_SHOP_BUYS = frozenset({"shop.card", "shop.relic", "shop.potion", "shop.remove"})
_DECK_PICKS = frozenset(
    {"select.upgrade", "select.remove", "select.transform", "select.duplicate", "select.enchant"}
)


def execute_best_option(client: ModClient, state: dict, result: StrategyResult | None) -> bool:
    """Execute `result`'s best-scored option (the engine's own order: best first).
    True if a concrete mod action was found and sent; False if the caller should fall
    back to the generic policy."""
    if result is None or not result.options:
        return False
    option = result.options[0].get("option") or {}
    kind = str(option.get("kind") or "")
    try:
        if kind in _DECK_PICKS and result.options[0].get("value") is None:
            # Nothing on this deck screen could be scored (an enchant: the screen does not
            # name the enchantment), so its first option is only the grid's first card --
            # a Strike, enchanted over and over (live 2026-10-05).
            return _pick_rarest(client, state)
        return _execute(client, state, kind, option)
    except BridgeError:
        return False


_RARITY = {"Basic": 0, "Common": 1, "Uncommon": 2, "Rare": 3}


def _pick_rarest(client: ModClient, state: dict) -> bool:
    """An unscored deck pick: the rarest card offered, the first of those -- a stand-in
    until the model can score it, which at least never spends it on a starter card when
    anything else is offered."""
    grid = state.get("card_select") or {}
    cards = [c for c in grid.get("cards") or [] if not c.get("selected") and "index" in c]
    if not cards:
        return False
    best = max(cards, key=lambda c: _RARITY.get(str(c.get("rarity")), 1))
    return _post(client, {"action": "select_card", "index": best["index"]})


def _execute(client: ModClient, state: dict, kind: str, option: dict) -> bool:
    if kind == "map.node":
        return _map_node(client, state, option)
    if kind == "card.skip":
        return _post(client, {"action": "skip_card_reward"})
    if kind == "card.reroll":
        return _post(client, {"action": "select_card_reward_alternative", "index": 0})
    if kind == "card.take":
        return _take_card_reward(client, option)
    if kind in ("reward.potion", "reward.relic"):
        return _claim_reward(client, state, kind)
    if kind in ("reward.potion_swap", "shop.potion_swap"):
        return _swap_potion(client, state, kind, option)
    if kind == "reward.card":
        # A card a Thieving Hopper stole, offered back: `action` is the item's index.
        return _index_action(client, option, "claim_reward", "index")
    if kind in _SHOP_BUYS:
        # The mod opens the shop's inventory on the first purchase and can answer "not
        # ready yet, retry" while it does (McpMod.Actions.cs ExecuteShopPurchase).
        for _ in range(3):
            if _index_action(client, option, "shop_purchase", "index"):
                return True
            time.sleep(0.5)
        return False
    if kind.startswith("rest.") and kind != "rest.leave":
        return _index_action(client, option, "choose_rest_option", "index")
    if kind == "event.option":
        return _index_action(client, option, "choose_event_option", "index")
    if kind in ("reward.proceed", "shop.leave", "rest.leave"):
        return _post(client, {"action": "proceed"})
    if kind == "select.offer_done":
        return _post(client, {"action": "confirm_selection"})
    if kind == "neow.relic":
        if state.get("state_type") == "event":
            # An Ancient's relic offer after Act 1 (Pael, live 2026-09-29) is an event
            # screen: select_relic does nothing there, choose_event_option takes it.
            options = (state.get("event") or {}).get("options") or []
            if any(o.get("index") == option.get("action") and o.get("is_locked") for o in options):
                return False  # a locked option is refused: the fallback takes an open one
            return _index_action(client, option, "choose_event_option", "index")
        return _index_action(client, option, "select_relic", "index")
    if kind == "select.offer" or kind in _DECK_PICKS:
        grid = state.get("card_select") or {}
        cards = grid.get("cards") or []
        chosen = sum(1 for card in cards if card.get("selected"))
        if chosen >= (grid.get("required_count") or grid.get("max_select") or 1):
            return _post(client, {"action": "confirm_selection"})
        if any(c.get("index") == option.get("action") and c.get("selected") for c in cards):
            return False  # already picked (a click would unselect it): the fallback picks anew
        return _index_action(client, option, "select_card", "index")
    return False


def _index_action(client: ModClient, option: dict, action: str, field: str) -> bool:
    """`option["action"]` is already the screen's own index (the training design's
    convention for a plain "pick one of these by position" option -- the same one
    `_take_card_reward` uses for `card.take`): `neow.relic` addresses the relic-select
    screen's own `select_relic` index (features.py: `Option("neow.relic",
    choice["index"], ...)`), `select.offer` a card_select screen's own `select_card`
    index (every card_select's own cards are built as `select.offer`, regardless of
    whether the screen is Neow's transform bonus, a rest site's Smith upgrade, a
    shop's removal or an event's transform -- bug 7/7)."""
    index = option.get("action")
    if not isinstance(index, int) or isinstance(index, bool):
        return False
    return _post(client, {"action": action, field: index})


def _map_node(client: ModClient, state: dict, option: dict) -> bool:
    node = option.get("node")
    options = (state.get("map") or {}).get("next_options") or []
    if not options or node is None:
        return False
    index = None
    if isinstance(node, list | tuple) and node:
        col = node[0]
        candidate = next(
            (o for o in options if o.get("col") == col or o.get("column") == col), None
        )
        index = candidate.get("index") if candidate else None
    if index is None:
        node_type = _node_type_name(node)
        candidate = next((o for o in options if o.get("type") == node_type), None)
        index = candidate.get("index") if candidate else None
    if index is None:
        return False
    # bug 3&4/7: waits for proof the click was seen before this decision is treated as
    # done, instead of firing choose_map_node and letting the caller act again on a
    # read that can still be the room the click has not actually left yet.
    navigator.act_and_await_change(client, {"action": "choose_map_node", "index": index}, state)
    return True


def _node_type_name(node: object) -> str | None:
    if isinstance(node, list | tuple) and len(node) >= 3:
        try:
            return _NODE_TYPE_NAMES.get(int(node[2]))
        except (TypeError, ValueError):
            return None
    return None


def _take_card_reward(client: ModClient, option: dict) -> bool:
    """`card.take`: `option["action"]` is already the offered card's on-screen index
    (`rl/run_train/features.py`'s `_card_option` sets it from the mod's own
    `card_reward.cards[i].index`), so unlike `map.node` this needs no lookup against
    `state` to place it -- `select_card_reward`'s `card_index` takes that same index
    directly (STS2MCP `ExecuteSelectCardReward`)."""
    index = option.get("action")
    if not isinstance(index, int) or isinstance(index, bool):
        return False
    return _post(client, {"action": "select_card_reward", "card_index": index})


def _claim_reward(client: ModClient, state: dict, kind: str) -> bool:
    """`reward.potion`/`reward.relic` only: gold, a potion or a relic off the
    `rewards` screen, none of which the workshop content mods grow past what the
    simulator knows (only Ironclad's card pool is bigger with them enabled)."""
    items = (state.get("rewards") or {}).get("items") or []
    wanted = {"reward.potion": "potion", "reward.relic": "relic"}[kind]
    if wanted == "potion" and navigator.potion_belt_full(state):
        return False  # can't be claimed: the fallback skips it and proceeds
    item = next((i for i in items if i.get("type") == wanted), None)
    if item is None or "index" not in item:
        return False
    return _post(client, {"action": "claim_reward", "index": item["index"]})


def _swap_potion(client: ModClient, state: dict, kind: str, option: dict) -> bool:
    """A potion taken over a full belt: throw away the held one the model named
    (`discard`, the game's slug), then claim or buy the new one at `option["action"]`."""
    index = option.get("action")
    held = (state.get("player") or {}).get("potions") or []
    slot = next(
        (p.get("slot", i) for i, p in enumerate(held) if isinstance(p, dict) and p.get("id") == option.get("discard")),
        None,
    )
    if slot is None or not isinstance(index, int) or isinstance(index, bool):
        return False
    if not _post(client, {"action": "discard_potion", "slot": slot}):
        return False
    if kind == "reward.potion_swap":
        return _post(client, {"action": "claim_reward", "index": index})
    return _post(client, {"action": "shop_purchase", "index": index})


def _post(client: ModClient, action: dict) -> bool:
    response = client.post_action(action)
    return not (isinstance(response, dict) and response.get("status") == "error")
