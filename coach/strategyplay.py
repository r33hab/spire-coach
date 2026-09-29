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

Still never maps a shop purchase (`shop.card`/`shop.relic`/`shop.potion`, the training
design's real kind names -- `rl/run_train/features.py` OPTION_KINDS; there is no
`shop.buy_*`) or a shop removal (`shop.remove`): unlike a card reward, a shop's offered
cards and their prices are not yet checked live end to end, so this module leaves the
whole shop screen (besides `shop.leave`) to the generic fallback, which never buys
anything, until that path is verified.

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


def execute_best_option(client: ModClient, state: dict, result: StrategyResult | None) -> bool:
    """Execute `result`'s best-scored option (the engine's own order: best first).
    True if a concrete mod action was found and sent; False if the caller should fall
    back to the generic policy."""
    if result is None or not result.options:
        return False
    option = result.options[0].get("option") or {}
    kind = str(option.get("kind") or "")
    try:
        return _execute(client, state, kind, option)
    except BridgeError:
        return False


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
    if kind == "select.offer":
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


def _post(client: ModClient, action: dict) -> bool:
    client.post_action(action)
    return True
