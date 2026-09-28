"""Tests for coach/strategyplay.py: mapping the strategy champion's best option to a
mod action, and falling back (returning False) for anything not mapped with
confidence."""

from __future__ import annotations

from coach.bridge.client import BridgeError
from coach.engine import StrategyResult
from coach.strategyplay import execute_best_option


class FakeClient:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.posted: list[dict] = []
        self._raises = raises

    def post_action(self, action: dict, **kwargs) -> dict:
        if self._raises is not None:
            raise self._raises
        self.posted.append(action)
        return {"status": "ok"}


def result_for(kind: str, **fields) -> StrategyResult:
    option = {"kind": kind, **fields}
    return StrategyResult(
        options=[{"option": option, "label": "x", "value": 1.0, "p_boss": None}], model={}
    )


def test_no_result_falls_back():
    assert execute_best_option(FakeClient(), {}, None) is False


def test_empty_options_falls_back():
    assert execute_best_option(FakeClient(), {}, StrategyResult(options=[], model={})) is False


def test_unmapped_kind_falls_back():
    client = FakeClient()
    assert execute_best_option(client, {}, result_for("select.upgrade", deck_index=0)) is False
    assert client.posted == []


def test_map_node_matched_by_column():
    client = FakeClient()
    state = {"map": {"next_options": [{"index": 0, "col": 1}, {"index": 1, "col": 3}]}}
    result = result_for("map.node", node=[3, 0, 1])
    assert execute_best_option(client, state, result) is True
    assert client.posted == [{"action": "choose_map_node", "index": 1}]


def test_map_node_matched_by_type_when_columns_do_not_match():
    client = FakeClient()
    state = {"map": {"next_options": [{"index": 0, "type": "Shop"}, {"index": 1, "type": "Elite"}]}}
    result = result_for("map.node", node=[99, 0, 2])  # type 2 = Elite
    assert execute_best_option(client, state, result) is True
    assert client.posted == [{"action": "choose_map_node", "index": 1}]


def test_map_node_no_match_falls_back():
    client = FakeClient()
    state = {"map": {"next_options": []}}
    assert execute_best_option(client, state, result_for("map.node", node=[0, 0, 1])) is False


def test_card_skip():
    client = FakeClient()
    assert execute_best_option(client, {}, result_for("card.skip")) is True
    assert client.posted == [{"action": "skip_card_reward"}]


def test_card_reroll():
    client = FakeClient()
    assert execute_best_option(client, {}, result_for("card.reroll")) is True
    assert client.posted == [{"action": "select_card_reward_alternative", "index": 0}]


def test_reward_potion_claims_the_potion_item():
    client = FakeClient()
    state = {"rewards": {"items": [{"index": 0, "type": "gold"}, {"index": 1, "type": "potion"}]}}
    assert execute_best_option(client, state, result_for("reward.potion")) is True
    assert client.posted == [{"action": "claim_reward", "index": 1}]


def test_reward_relic_with_no_relic_offered_falls_back():
    client = FakeClient()
    state = {"rewards": {"items": [{"index": 0, "type": "gold"}]}}
    assert execute_best_option(client, state, result_for("reward.relic")) is False


def test_card_take_selects_the_offered_card_by_its_screen_index():
    # option["action"] is the training design's own address for the option (DESIGN.md
    # 3.4 / rl/run_train/features.py's Option), which for card.take is already the
    # card_reward screen's own card index -- the same one select_card_reward wants.
    client = FakeClient()
    assert execute_best_option(client, {}, result_for("card.take", action=1)) is True
    assert client.posted == [{"action": "select_card_reward", "card_index": 1}]


def test_card_take_with_no_action_index_falls_back():
    client = FakeClient()
    assert execute_best_option(client, {}, result_for("card.take")) is False
    assert client.posted == []


def test_reward_proceed_shop_leave_rest_leave_all_proceed():
    for kind in ("reward.proceed", "shop.leave", "rest.leave"):
        client = FakeClient()
        assert execute_best_option(client, {}, result_for(kind)) is True
        assert client.posted == [{"action": "proceed"}]


def test_select_offer_done_confirms():
    client = FakeClient()
    assert execute_best_option(client, {}, result_for("select.offer_done")) is True
    assert client.posted == [{"action": "confirm_selection"}]


def test_bridge_error_falls_back_instead_of_raising():
    client = FakeClient(raises=BridgeError("nope"))
    assert execute_best_option(client, {}, result_for("card.skip")) is False


def test_no_shop_purchase_kind_is_ever_mapped():
    # The training design's real kind names (rl/run_train/features.py OPTION_KINDS) are
    # "shop.card"/"shop.relic"/"shop.potion"/"shop.remove" -- not "shop.buy_*", which
    # never appears; this test used to assert against kinds the engine never sends.
    # Shop buys and card removal are not yet checked live end to end (task item 4), so
    # only shop.leave is mapped; the rest still falls back to the generic policy, which
    # never buys or removes anything either.
    client = FakeClient()
    for kind in ("shop.card", "shop.relic", "shop.potion", "shop.remove"):
        assert execute_best_option(client, {}, result_for(kind, deck_index=0)) is False
    assert client.posted == []
