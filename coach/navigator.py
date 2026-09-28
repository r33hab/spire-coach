"""Everything about a run that is not a combat decision: the preconditions fight-lab
and autopilot both check before they touch the game, starting and leaving a run on
modded profile 2, and the generic "first non-skip option, then proceed" fallback for a
non-combat screen. Shared by `fight-lab` and `autopilot` (the coordinator's
instruction not to duplicate recovery): fight-lab uses the run lifecycle and the
preconditions; autopilot uses those plus the generic fallback for whichever screen its
strategy mapping (`coach.strategyplay`) does not cover.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from coach.bridge.client import BridgeError, ModClient
from coach.config import CONF_FILE_NAME, read_allow_debug_actions

_TAKEABLE_REWARD_TYPES = frozenset({"gold", "potion", "relic"})
_DEFAULT_TIMEOUT_S = 30.0
_DEFAULT_POLL_S = 0.5


class NavigatorError(Exception):
    """The game did not respond the way the menu-walking code expects."""


# --- preconditions ---------------------------------------------------------------


def check_preconditions(client: ModClient, config) -> list[str]:
    """Every problem stopping the lab from running unattended, checked in the order
    the spec lists them; empty means clear to go. `config` is a `coach.config.Config`
    (its `mods_dir` is where `STS2_MCP.conf` lives)."""
    problems: list[str] = []
    try:
        client.hello()
    except BridgeError as exc:
        problems.append(
            f"the game is not running with the mod loaded (could not reach it: {exc})"
        )
        return problems  # nothing past this is checkable without the mod

    if not read_allow_debug_actions(config.mods_dir):
        conf_path = config.mods_dir / CONF_FILE_NAME
        problems.append(
            "debug actions are disabled. Close the game, then set "
            f'"allow_debug_actions": true in {conf_path}, and relaunch the game.'
        )

    try:
        profiles = client.profiles()
    except BridgeError as exc:
        problems.append(f"could not read the active save profile: {exc}")
    else:
        current = profiles.get("current_profile_id")
        if current != 2:
            problems.append(
                f"the active save profile is {current!r}, not modded profile 2. "
                "From the game's main menu, open the profile screen and switch to "
                "profile 2, then run the lab again."
            )
    return problems


# --- run lifecycle -----------------------------------------------------------------


def session_header(state: dict) -> dict:
    """What `fight-lab`/`autopilot` record at the very start of a session log: the
    mod's own `loaded_mods` list (STS2MCP's `m2-runs` branch; an older installed mod
    may not send it, and the field is `None` then) plus the version fields every
    payload carries. A workshop content mod changes what a roll can draw, so a session
    recorded with one loaded is not comparable to one without; this is how the log
    says which case it was, without this module needing to know what any mod does."""
    return {
        "loaded_mods": state.get("loaded_mods"),
        "mod_version": state.get("mod_version"),
        "mod_commit": state.get("mod_commit"),
        "schema_version": state.get("schema_version"),
    }


def hp_of(state: dict) -> int | None:
    hp = (state.get("player") or {}).get("hp")
    return hp if isinstance(hp, int) and not isinstance(hp, bool) else None


def act_of(state: dict) -> int | None:
    act = (state.get("run") or {}).get("act")
    return act if isinstance(act, int) and not isinstance(act, bool) else None


def embark_run(
    client: ModClient,
    *,
    ascension: int,
    character: str = "Ironclad",
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> dict:
    """From the main menu, start a fresh singleplayer/standard run as `character` at
    `ascension`. `menu_select`'s `ascension` field sets the character-select screen's
    ascension panel before embarking (STS2MCP McpMod.Actions.cs `ApplyAscension`)."""
    _select(client, "singleplayer")
    _select(client, "standard")
    state = poll_until(
        client,
        lambda s: s.get("menu_screen") == "character_select" and not s.get("selection_busy"),
        timeout_s=timeout_s,
    )
    options = [o for o in state.get("options") or [] if character.lower() in str(o).lower()]
    if not options:
        raise NavigatorError(f"no {character!r} option on character select: {state.get('options')}")
    client.post_action({"action": "menu_select", "option": options[0], "ascension": ascension})
    state = poll_until(
        client, lambda s: s.get("selected_character") is not None, timeout_s=timeout_s
    )
    confirm = "confirm" if "confirm" in (state.get("options") or []) else "embark"
    _select(client, confirm)
    return poll_until(client, lambda s: act_of(s) is not None, timeout_s=timeout_s)


def enter_act(client: ModClient, act: int, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> dict:
    """Fast-forward the current run to `act` (1-3) with `debug_enter_next_act`, so a
    fresh run can reach an Act 2/3 encounter without clearing Act 1 first, and so its
    own `run.act` matches the encounter (spec: fights are picked from the real
    per-act roster; a mismatched act would miscount them in the fidelity scoreboard)."""
    state = client.get_state()
    current = act_of(state)
    if current is None:
        raise NavigatorError("no run in progress to advance")
    for _ in range(max(0, act - current)):
        client.post_action({"action": "debug_enter_next_act"})
        before = current
        state = poll_until(
            client, lambda s, before=before: act_of(s) != before, timeout_s=timeout_s
        )
        current = act_of(state)
    return state


def leave_run(client: ModClient, state: dict, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> dict:
    """Get back to the main menu from wherever a fight or a run just ended, cleanly:
    `return_to_main_menu`'s save-and-quit path normally, or the game-over screen's own
    `main_menu` option when the run is already over. Never raises: a menu action that
    fails still gets polled for, so a slow transition is not mistaken for a stuck one.
    """
    if state.get("state_type") == "game_over":
        action = {"action": "menu_select", "option": "main_menu"}
    else:
        action = {"action": "return_to_main_menu"}
    try:
        client.post_action(action)
    except BridgeError:
        pass
    return poll_until(client, lambda s: s.get("state_type") == "menu", timeout_s=timeout_s)


# --- generic non-combat fallback ---------------------------------------------------


def advance_one_step(client: ModClient, state: dict) -> dict:
    """One simple, safe action on a non-combat screen: the "first non-skip option,
    then proceed" fallback the autopilot spec calls for when the strategy champion has
    no answer (no champion exported, an unscored screen, or a request error). Mirrors
    the throwaway drivers this module replaces (coach_drive.py, task12/drive.py) --
    not good play, just forward progress that never touches a fight's own decisions.
    """
    state_type = state.get("state_type")
    if state_type == "rewards":
        items = (state.get("rewards") or {}).get("items") or []
        take = [item for item in items if item.get("type") in _TAKEABLE_REWARD_TYPES]
        _try(client, {"action": "claim_reward", "index": take[0]["index"]}) if take else _try(
            client, {"action": "proceed"}
        )
    elif state_type in ("card_reward", "card_select"):
        _try(client, {"action": "skip_card_reward"})
    elif state_type == "map":
        options = (state.get("map") or {}).get("next_options") or []
        pick = next((o for o in options if o.get("type") in ("Monster", "Elite")), None)
        pick = pick or (options[0] if options else None)
        if pick is not None:
            _try(client, {"action": "choose_map_node", "index": pick["index"]})
    elif state_type == "treasure":
        _try(client, {"action": "claim_treasure_relic", "index": 0})
        _try(client, {"action": "proceed"})
    elif state_type == "shop":
        _try(client, {"action": "proceed"})
    elif state_type == "rest_site":
        options = (state.get("rest_site") or {}).get("options") or []
        if options:
            _try(client, {"action": "choose_rest_option", "index": 0})
        _try(client, {"action": "proceed"})
    elif state_type == "event":
        options = (state.get("event") or {}).get("options") or []
        if options:
            _try(client, {"action": "choose_event_option", "index": options[0].get("index", 0)})
    elif state_type == "relic_select":
        _try(client, {"action": "select_relic", "index": 0})
    elif state_type == "game_over":
        _try(client, {"action": "menu_select", "option": "main_menu"})
    else:
        _try(client, {"action": "proceed"})
    return client.get_state()


def _try(client: ModClient, action: dict) -> dict | None:
    try:
        return client.post_action(action)
    except BridgeError:
        return None


def _select(client: ModClient, option: str, **fields: object) -> dict:
    return client.post_action({"action": "menu_select", "option": option, **fields})


def poll_until(
    client: ModClient,
    predicate: Callable[[dict], bool],
    *,
    timeout_s: float,
    poll_s: float = _DEFAULT_POLL_S,
) -> dict:
    deadline = time.monotonic() + timeout_s
    state = client.get_state()
    while not predicate(state):
        if time.monotonic() > deadline:
            return state
        time.sleep(poll_s)
        state = client.get_state()
    return state
