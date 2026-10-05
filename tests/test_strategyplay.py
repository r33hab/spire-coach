"""Tests for coach/strategyplay.py: mapping the strategy champion's best option to a
mod action, and falling back (returning False) for anything not mapped with
confidence."""

from __future__ import annotations

import time

import pytest

from coach.bridge.client import BridgeError
from coach.engine import StrategyResult
from coach.strategyplay import execute_best_option


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


class FakeClient:
    def __init__(self, *, raises: Exception | None = None, reads: list[dict] | None = None) -> None:
        self.posted: list[dict] = []
        self._raises = raises
        # map.node's own follow-up wait (act_and_await_change) reads this once right
        # after posting; a settled, different-from-baseline state resolves the wait on
        # its first read, same as every other test here assumed before that wait
        # existed. `reads`, if given, is consumed one at a time instead (repeating the
        # last entry), for tests of the wait itself.
        self._reads = reads or [{"state_type": "monster", "action_log_seq": 1, "is_resolving": False}]
        self._read_i = 0

    def post_action(self, action: dict, **kwargs) -> dict:
        if self._raises is not None:
            raise self._raises
        self.posted.append(action)
        return {"status": "ok"}

    def get_state(self) -> dict:
        state = self._reads[min(self._read_i, len(self._reads) - 1)]
        self._read_i += 1
        return state


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
    assert execute_best_option(client, {}, result_for("sphere.cell", action=0)) is False
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


def test_map_node_waits_past_a_stale_read_and_never_double_clicks():
    # bug 3&4/7: the same stale-read race as the generic fallback's own choose_map_node
    # -- a read straight after the click can still show the room the click has not
    # left yet, and acting on it again is what jams the game.
    map_block = {"map": {"next_options": [{"index": 0, "type": "Monster"}]}}
    baseline = {**map_block, "state_type": "map", "action_log_seq": 5, "is_resolving": False}
    stale = dict(baseline)  # unchanged from baseline -- must not be trusted
    settled = {"state_type": "monster", "action_log_seq": 6, "is_resolving": False}
    client = FakeClient(reads=[stale, settled])

    assert execute_best_option(client, baseline, result_for("map.node", node=[0, 0, 1])) is True
    assert client.posted == [{"action": "choose_map_node", "index": 0}]


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


def test_neow_relic_selects_the_offered_relic_by_its_screen_index():
    # rl/run_train/features.py: Option("neow.relic", choice["index"], relic=relic) --
    # action is already the relic-select screen's own index, same convention as
    # card.take (bug 7/7: Neow's relic choice was unmapped, generic-fallback only).
    client = FakeClient()
    assert execute_best_option(client, {}, result_for("neow.relic", action=1)) is True
    assert client.posted == [{"action": "select_relic", "index": 1}]


def test_neow_relic_with_no_action_index_falls_back():
    client = FakeClient()
    assert execute_best_option(client, {}, result_for("neow.relic")) is False
    assert client.posted == []


def test_select_offer_takes_the_offered_card_by_its_screen_index():
    # features.py: every card_select screen's own cards -- Neow's transform/remove
    # bonus, a rest site's Smith upgrade, a shop's removal, an event's transform --
    # are uniformly select.offer (_card_option("select.offer", card, ...)), action
    # already the card_select screen's own index (bug 7/7: previously unmapped, so
    # every one of these fell to the generic "first card" fallback instead of the
    # champion's actual pick).
    client = FakeClient()
    assert execute_best_option(client, {}, result_for("select.offer", action=0)) is True
    assert client.posted == [{"action": "select_card", "index": 0}]


def test_select_offer_with_no_action_index_falls_back():
    client = FakeClient()
    assert execute_best_option(client, {}, result_for("select.offer")) is False
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


def test_shop_buys_and_the_removal_are_bought_by_the_shop_item_index():
    # Live 2026-10-05: every shop was left without buying anything -- these kinds fell
    # to the fallback, which only proceeds. `action` is the shop item's own index
    # (features.py `_live_shop`), what `shop_purchase` takes.
    for kind in ("shop.card", "shop.relic", "shop.potion", "shop.remove"):
        client = FakeClient()
        assert execute_best_option(client, {}, result_for(kind, action=3, deck_index=0)) is True
        assert client.posted == [{"action": "shop_purchase", "index": 3}]


def test_rest_options_are_the_models_pick_not_the_first_enabled_one():
    for kind in ("rest.heal", "rest.smith", "rest.lift", "rest.dig"):
        client = FakeClient()
        assert execute_best_option(client, {}, result_for(kind, action=1)) is True
        assert client.posted == [{"action": "choose_rest_option", "index": 1}]


def test_event_options_are_the_models_pick_not_the_first_open_one():
    client = FakeClient()
    result = result_for("event.option", action=2, event=[17, 2, 0])
    assert execute_best_option(client, {}, result) is True
    assert client.posted == [{"action": "choose_event_option", "index": 2}]


def test_deck_screens_take_the_models_card():
    # A rest site's Smith, a shop's or an event's removal, a transform: features.py
    # names these screens' cards select.upgrade/select.remove/..., not select.offer, so
    # they fell to the fallback's first card -- Smith upgraded whatever came first.
    for kind in ("select.upgrade", "select.remove", "select.transform", "select.duplicate"):
        client = FakeClient()
        state = {"card_select": {"cards": [{"index": 0}, {"index": 4}], "max_select": 1}}
        assert execute_best_option(client, state, result_for(kind, action=4)) is True
        assert client.posted == [{"action": "select_card", "index": 4}]


def test_an_action_the_mod_refuses_falls_back():
    class Refusing(FakeClient):
        def post_action(self, action: dict, **kwargs) -> dict:
            self.posted.append(action)
            return {"status": "error", "error": "Not enough gold"}

    client = Refusing()
    assert execute_best_option(client, {}, result_for("rest.heal", action=0)) is False
    client = Refusing()  # a shop buy is tried 3 times (the inventory opens on the first)
    assert execute_best_option(client, {}, result_for("shop.card", action=0)) is False
    assert len(client.posted) == 3


def test_a_shop_buy_waits_for_the_inventory_to_open():
    class Opening(FakeClient):
        def post_action(self, action: dict, **kwargs) -> dict:
            self.posted.append(action)
            if len(self.posted) == 1:
                return {"status": "error", "error": "Shop inventory not ready yet"}
            return {"status": "ok"}

    client = Opening()
    assert execute_best_option(client, {}, result_for("shop.relic", action=5)) is True
    assert client.posted == [{"action": "shop_purchase", "index": 5}] * 2


def test_an_ancient_relic_offer_on_an_event_screen_uses_choose_event_option():
    # Live (2026-09-29, Pael at the start of Act 2): the offer is an event screen, where
    # select_relic does nothing and the autopilot stalled; choose_event_option takes it.
    client = FakeClient()
    assert execute_best_option(client, {"state_type": "event"}, result_for("neow.relic", action=1))
    assert client.posted == [{"action": "choose_event_option", "index": 1}]


def test_neows_own_relic_screen_still_uses_select_relic():
    client = FakeClient()
    execute_best_option(client, {"state_type": "relic_select"}, result_for("neow.relic", action=0))
    assert client.posted == [{"action": "select_relic", "index": 0}]


def test_a_potion_reward_with_a_full_belt_is_left_to_the_fallback():
    # Live (2026-09-29, floor 39): a Foul Potion reward with both slots full; claiming
    # it again and again stalled the rewards screen.
    state = {"state_type": "rewards", "player": {"potions": ["FOUL_POTION", "FOUL_POTION"], "max_potion_slots": 2},
             "rewards": {"items": [{"index": 0, "type": "potion"}], "can_proceed": True}}
    client = FakeClient()
    assert execute_best_option(client, state, result_for("reward.potion")) is False
    assert client.posted == []


def test_a_stolen_card_offered_back_is_claimed_when_the_model_takes_it():
    client = FakeClient()
    assert execute_best_option(client, {}, result_for("reward.card", action=2)) is True
    assert client.posted == [{"action": "claim_reward", "index": 2}]


def test_a_potion_swap_throws_away_the_named_potion_then_takes_the_new_one():
    state = {"player": {"potions": [
        {"id": "BLOCK_POTION", "slot": 0}, {"id": "FIRE_POTION", "slot": 1},
    ]}}
    client = FakeClient()
    result = result_for("reward.potion_swap", action=3, discard="FIRE_POTION")
    assert execute_best_option(client, state, result) is True
    assert client.posted == [
        {"action": "discard_potion", "slot": 1},
        {"action": "claim_reward", "index": 3},
    ]
    client = FakeClient()
    result = result_for("shop.potion_swap", action=6, discard="BLOCK_POTION")
    assert execute_best_option(client, state, result) is True
    assert client.posted == [
        {"action": "discard_potion", "slot": 0},
        {"action": "shop_purchase", "index": 6},
    ]
    # A potion no longer held: nothing is thrown away.
    client = FakeClient()
    result = result_for("reward.potion_swap", action=3, discard="SWIFT_POTION")
    assert execute_best_option(client, state, result) is False
    assert client.posted == []


def test_an_unscored_deck_pick_takes_the_rarest_card_not_the_first():
    # Live 2026-10-05: an enchant screen is never scorable, and its first option -- the
    # grid's first card, a Strike -- was enchanted every time.
    state = {"card_select": {"cards": [
        {"index": 0, "id": "STRIKE_IRONCLAD", "rarity": "Basic"},
        {"index": 1, "id": "POMMEL_STRIKE", "rarity": "Common"},
        {"index": 2, "id": "DEMON_FORM", "rarity": "Rare"},
    ]}}
    result = StrategyResult(
        options=[{"option": {"kind": "select.enchant", "action": 0}, "label": "x", "value": None, "p_boss": None}],
        model={},
    )
    client = FakeClient()
    assert execute_best_option(client, state, result) is True
    assert client.posted == [{"action": "select_card", "index": 2}]
