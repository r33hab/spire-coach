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

Never maps a card reward or a shop purchase (`card.take` and every `shop.buy_*`):
workshop content mods (Watcher, The Shape, Multiplayer Expanded) grow Ironclad's card
pool past what the simulator, the converter and the strategy champion were trained on,
so an offered card or shop item may be one none of them can score. The generic
fallback's own card-reward and shop handling never takes or buys anything either, so a
modded card is never picked regardless of which policy is driving.
"""

from __future__ import annotations

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
    # "card.take" is deliberately NOT mapped: workshop content mods (Watcher, The Shape,
    # Multiplayer Expanded) grow Ironclad's card pool past what the simulator, the
    # converter and the strategy champion know, so an offered card reward may be one
    # none of them can score. Falling back to the generic policy's unconditional
    # skip_card_reward is what keeps a modded card from ever being taken; the same
    # reasoning is why no shop.buy_* kind is mapped here either (the generic policy
    # only ever leaves a shop, never buys).
    if kind in ("reward.potion", "reward.relic"):
        return _claim_reward(client, state, kind)
    if kind in ("reward.proceed", "shop.leave", "rest.leave"):
        return _post(client, {"action": "proceed"})
    if kind == "select.offer_done":
        return _post(client, {"action": "confirm_selection"})
    return False


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
    return _post(client, {"action": "choose_map_node", "index": index})


def _node_type_name(node: object) -> str | None:
    if isinstance(node, list | tuple) and len(node) >= 3:
        try:
            return _NODE_TYPE_NAMES.get(int(node[2]))
        except (TypeError, ValueError):
            return None
    return None


def _claim_reward(client: ModClient, state: dict, kind: str) -> bool:
    """`reward.potion`/`reward.relic` only: gold, a potion or a relic off the
    `rewards` screen, none of which the workshop content mods grow past what the
    simulator knows (only Ironclad's card pool is bigger with them enabled)."""
    items = (state.get("rewards") or {}).get("items") or []
    wanted = {"reward.potion": "potion", "reward.relic": "relic"}[kind]
    item = next((i for i in items if i.get("type") == wanted), None)
    if item is None or "index" not in item:
        return False
    return _post(client, {"action": "claim_reward", "index": item["index"]})


def _post(client: ModClient, action: dict) -> bool:
    client.post_action(action)
    return True
