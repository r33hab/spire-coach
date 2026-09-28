"""A minimal, in-process fake of the STS2MCP mod's menu/run state machine, driven by
the same actions coach.navigator posts. Not a mock of individual calls: it tracks just
enough state (menu screen, run act, hp) to answer get_state()/post_action() the way
the real mod would for the flows navigator.py drives, so a test exercises real control
flow (poll-until loops included) instead of a canned script of returns.
"""

from __future__ import annotations

import copy

from coach.bridge.client import BridgeError


class ScriptedMod:
    def __init__(self, *, current_profile_id: int = 2, hello_ok: bool = True) -> None:
        self.current_profile_id = current_profile_id
        self.hello_ok = hello_ok
        self.state_type = "menu"
        self.menu_screen = "main"
        self.options: list[str] = []
        self.selection_busy = False
        self.selected_character: str | None = None
        self.act: int | None = None
        self.floor = 1
        self.hp = 80
        self.max_hp = 80
        self.next_map_options = [{"index": 0, "type": "Monster"}]
        self.after_map_choice = "rewards"  # state_type choose_map_node lands on
        self.rewards_items: list[dict] = []
        self.card_select_cards: list[dict] = []
        self.rest_site_options: list[dict] = []
        self.event_options: list[dict] = []
        self.posted: list[dict] = []
        self.extra_state: dict = {}
        self.loaded_mods: list[dict] | None = None
        # name -> tier ("monster"/"elite"/"boss"), consulted by debug_start_encounter;
        # an unlisted name defaults to "monster".
        self.encounter_tiers: dict[str, str] = {}
        # Bug 6/7: right after a game restart, character_select can be reached before
        # its roster is populated ("no Ironclad option ... []"). 0 (default) populates
        # immediately, as every other test here assumes; a positive count holds
        # `options` empty for that many get_state() reads first.
        self.character_select_delay_reads = 0
        self._character_select_reads_seen = 0

    # --- ModClient's interface ------------------------------------------------

    def hello(self) -> dict:
        if not self.hello_ok:
            raise BridgeError("mod unreachable")
        return {"status": "ok"}

    def profiles(self) -> dict:
        return {"current_profile_id": self.current_profile_id, "profiles": []}

    def get_state(self) -> dict:
        if (
            self.menu_screen == "character_select"
            and not self.options
            and self.character_select_delay_reads > 0
        ):
            self._character_select_reads_seen += 1
            if self._character_select_reads_seen >= self.character_select_delay_reads:
                self.options = ["Ironclad"]
        combat = self.state_type in ("monster", "elite", "boss")
        state: dict = {
            "state_type": self.state_type,
            "menu_screen": self.menu_screen,
            "options": list(self.options),
            "selection_busy": self.selection_busy,
            "selected_character": self.selected_character,
            "in_combat": combat,
            "player": {"hp": self.hp, "max_hp": self.max_hp},
        }
        if combat:
            # Settled from the first read: no start-sequence/animation to wait out,
            # since nothing here drives real turns (coach.autoplay is faked in these
            # orchestration tests -- see test_autoplay.py for the real combat loop).
            state["is_resolving"] = False
            state["battle"] = {"is_play_phase": True, "turn": "player", "round": 1, "enemies": []}
        if self.act is not None:
            state["run"] = {"act": self.act, "floor": self.floor}
        if self.state_type == "map":
            state["map"] = {"next_options": copy.deepcopy(self.next_map_options)}
        if self.state_type == "rewards":
            state["rewards"] = {"items": copy.deepcopy(self.rewards_items), "can_proceed": True}
        if self.state_type == "card_select":
            state["card_select"] = {"cards": copy.deepcopy(self.card_select_cards)}
        if self.state_type == "rest_site":
            state["rest_site"] = {
                "options": copy.deepcopy(self.rest_site_options),
                "can_proceed": False,
            }
        if self.state_type == "event":
            state["event"] = {"options": copy.deepcopy(self.event_options)}
        if self.state_type == "game_over":
            state["game_over"] = {"message": "Run ended.", "options": ["main_menu"]}
        if self.state_type == "menu":
            state["loaded_mods"] = self.loaded_mods
        state.update(self.extra_state)
        return state

    def post_action(self, action: dict, **kwargs) -> dict:
        self.posted.append(action)
        name = action.get("action")
        handler = getattr(self, f"_do_{name}", None)
        if handler is not None:
            return handler(action)
        return {"status": "ok"}

    # --- action handlers --------------------------------------------------------

    def _do_menu_select(self, action: dict) -> dict:
        option = action.get("option")
        if option == "singleplayer":
            self.menu_screen = "singleplayer"
        elif option == "standard":
            self.menu_screen = "character_select"
            self._character_select_reads_seen = 0
            self.options = [] if self.character_select_delay_reads > 0 else ["Ironclad"]
        elif option == "Ironclad":
            self.selected_character = "Ironclad"
            self.options = ["confirm"]
            if "ascension" in action:
                self.extra_state["ascension_set"] = action["ascension"]
        elif option in ("confirm", "embark"):
            self.state_type = "map"
            self.act = 1
            self.floor = 1
            self.selected_character = None
            self.selection_busy = False
        elif option == "main_menu":
            self._to_main_menu()
        return {"status": "ok"}

    def _do_return_to_main_menu(self, action: dict) -> dict:
        self._to_main_menu()
        return {"status": "ok"}

    def _to_main_menu(self) -> None:
        self.state_type = "menu"
        self.menu_screen = "main"
        self.act = None
        self.options = []
        self.selected_character = None

    def _do_debug_enter_next_act(self, action: dict) -> dict:
        if self.act is not None:
            self.act += 1
        return {"status": "ok"}

    def _do_debug_start_encounter(self, action: dict) -> dict:
        self.state_type = self.encounter_tiers.get(action.get("encounter"), "monster")
        return {"status": "ok"}

    def _do_choose_map_node(self, action: dict) -> dict:
        self.state_type = self.after_map_choice
        return {"status": "ok"}

    def _do_proceed(self, action: dict) -> dict:
        if self.state_type == "rewards":
            self.state_type = "map"
        return {"status": "ok"}
