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


def _option_name(option: object) -> str:
    """The main menu's own options are plain strings; a popup's and the character
    select screen's are `{"name": ..., "enabled": ...}` dicts (confirmed live: a
    popup's `yes`/`no`, and character select's ids like `IRONCLAD`). Either way, this
    is the string `menu_select`'s own `option` field takes."""
    if isinstance(option, dict):
        return str(option.get("name", option))
    return str(option)


def _option_names(state: dict) -> list[str]:
    return [_option_name(o) for o in state.get("options") or []]


def _abandon_any_run_in_progress(client: ModClient, *, timeout_s: float) -> None:
    """`return_to_main_menu` (what `leave_run` uses after a normal fight) saves the run
    for `continue`, so the main menu offers `continue`/`abandon_run` instead of
    `singleplayer` until it is abandoned -- confirmed live, not from the docs. A fresh
    run needs it gone first; the confirmation popup's `yes` finishes it."""
    state = client.get_state()
    if "abandon_run" not in _option_names(state):
        return
    client.post_action({"action": "menu_select", "option": "abandon_run"})
    state = poll_until(
        client, lambda s: "yes" in _option_names(s), timeout_s=timeout_s
    )
    if "yes" in _option_names(state):
        client.post_action({"action": "menu_select", "option": "yes"})
        poll_until(client, lambda s: "singleplayer" in _option_names(s), timeout_s=timeout_s)


def embark_run(
    client: ModClient,
    *,
    ascension: int,
    character: str = "Ironclad",
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> dict:
    """From the main menu, start a fresh singleplayer/standard run as `character` at
    `ascension`, abandoning a saved-but-unfinished run first if there is one.
    `menu_select`'s `ascension` field sets the character-select screen's ascension
    panel before embarking (STS2MCP McpMod.Actions.cs `ApplyAscension`)."""
    _abandon_any_run_in_progress(client, timeout_s=timeout_s)
    _select(client, "singleplayer")
    _select(client, "standard")
    state = poll_until(
        client,
        lambda s: s.get("menu_screen") == "character_select" and not s.get("selection_busy"),
        timeout_s=timeout_s,
    )
    names = _option_names(state)
    match = next((name for name in names if character.upper() in name.upper()), None)
    if match is None:
        raise NavigatorError(f"no {character!r} option on character select: {names}")
    client.post_action({"action": "menu_select", "option": match, "ascension": ascension})
    state = poll_until(
        client, lambda s: s.get("selected_character") is not None, timeout_s=timeout_s
    )
    confirm = "confirm" if "confirm" in _option_names(state) else "embark"
    _select(client, confirm)
    state = poll_until(client, lambda s: act_of(s) is not None, timeout_s=timeout_s)
    return _reach_the_map(client, state, timeout_s=timeout_s)


def _reach_the_map(client: ModClient, state: dict, *, timeout_s: float) -> dict:
    """A fresh run opens on Neow's event, not the map (confirmed live: Ironclad's Act 1
    start is a `NEOW` event with a reward pick, then its own single `Proceed` option).
    `debug_start_encounter` needs a real room to override (see `leave_the_map`), and
    there is none until the map exists, so this clicks through Neow's own options
    (first pick, then proceed) until the map appears or there is nothing left to click.
    A fixed, generous wait between clicks rather than a settle predicate: the mod's own
    docs warn the event id names its NEXT page before that page's own option list does,
    so "the state changed" is not by itself proof the click was seen."""
    for _ in range(5):
        if state.get("state_type") != "event":
            return state
        options = (state.get("event") or {}).get("options") or []
        if not options:
            return state
        client.post_action({"action": "choose_event_option", "index": options[0].get("index", 0)})
        time.sleep(1.5)
        state = client.get_state()
    return state


def leave_the_map(client: ModClient, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> dict:
    """Get off the map into a real room with a real node click, if the map is open.

    Confirmed live: calling `debug_start_encounter` while the map screen is the one
    open silently masks its own room-enter behind `state_type: "map"` forever after --
    the underlying combat starts for real (hand, energy and battle all update) but is
    never reported, which makes it useless to anything reading the mod's state. A real
    `choose_map_node` closes the map and reports whatever room it leads to;
    `debug_start_encounter` is then free to override that room with the one actually
    wanted. Which node is clicked does not matter, since it is about to be replaced.
    """
    state = client.get_state()
    if state.get("state_type") != "map":
        return state
    options = (state.get("map") or {}).get("next_options") or []
    if not options:
        raise NavigatorError("on the map with no next_options to leave it by")
    client.post_action({"action": "choose_map_node", "index": options[0]["index"]})
    return poll_until(client, lambda s: s.get("state_type") != "map", timeout_s=timeout_s)


def enter_act(
    client: ModClient, act: int, *, timeout_s: float = _DEFAULT_TIMEOUT_S, max_attempts: int = 6
) -> dict:
    """Fast-forward the current run to `act` (1-3) with `debug_enter_next_act`, so a
    fresh run can reach an Act 2/3 encounter without clearing Act 1 first, and so its
    own `run.act` matches the encounter (spec: fights are picked from the real per-act
    roster; a mismatched act would miscount them in the fidelity scoreboard).

    Confirmed live, and not consistently reproducible enough to name a single cause:
    one `debug_enter_next_act` call can silently not take (the response still says
    `"ok"`), most often but not only when it follows an earlier one too closely --
    `RunManager.EnterNextAct`'s own room/map generation likely keeps running on the
    main thread after `run.act` itself has already ticked over. Rather than one call
    per act trusted on faith, this re-reads `run.act` after each one and keeps calling
    until it actually reads the wanted act or `max_attempts` runs out, which is what
    catches a call that did not take -- the same request, tried again, plainly does.

    Also confirmed live: a fight-lab session that abandoned a run right after a
    debug-started boss win and immediately embarked the next one hit a
    NullReferenceException inside that same call (mod-side race fixed in STS2MCP, but
    worth tolerating here too rather than trusting it is gone for good) -- the mod
    answered with an HTTP failure `post_action` raises as `BridgeError` instead of the
    usual `{"status": "ok"}` that just did not take. One bad attempt must not spend the
    whole retry budget: it is treated the same as a call that silently did not take,
    not raised immediately, so a later attempt still gets its turn.
    """
    state = client.get_state()
    current = act_of(state)
    if current is None:
        raise NavigatorError("no run in progress to advance")
    for _ in range(max_attempts):
        if current >= act:
            return state
        try:
            client.post_action({"action": "debug_enter_next_act"})
        except BridgeError:
            time.sleep(3.0)
            continue
        before = current
        state = poll_until(
            client, lambda s, before=before: (act_of(s) or before) != before, timeout_s=timeout_s
        )
        current = act_of(state) or current
        time.sleep(3.0)  # let EnterNextAct's own generation finish before trusting this
    if current < act:
        raise NavigatorError(f"debug_enter_next_act did not reach act {act} (stuck at {current})")
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
    elif state_type == "card_reward":
        _try(client, {"action": "skip_card_reward"})
    elif state_type == "card_select":
        _select_first_card(client, state)
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
        # Real captures (data/runs/): each option carries its own is_enabled (HEAL is
        # disabled at full HP; SMITH is disabled with no upgradable card left). The
        # first option in the list is not always the first usable one, and
        # choose_rest_option on a disabled one is a no-op the mod answers with an
        # error, not a BridgeError this module could catch (McpMod.Actions.cs
        # ExecuteChooseRestOption) -- picking blindly by position silently wasted the
        # rest site whenever that happened.
        pick = next((o for o in options if o.get("is_enabled", True)), None)
        if pick is not None:
            _try(client, {"action": "choose_rest_option", "index": pick["index"]})
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


def _select_first_card(client: ModClient, state: dict) -> None:
    """The non-combat `card_select` grid (rest-site Smith's upgrade pick, a shop's
    removal, an event's transform, Neow's transform-a-card bonus): pick the first
    offered card, then confirm.

    Confirmed live (real captures under data/runs/, screen_type "upgrade"/"select"/
    "simple_select"/"grid"): this state_type is a different screen from `card_reward`
    (STS2MCP's `NCardGridSelectionScreen`/`select_card`+`confirm_selection`, not
    `NCardRewardSelectionScreen`/`skip_card_reward`) -- sending `skip_card_reward` here
    has no handler on this screen and left it up forever. `select_card`'s own
    confirmation can close the screen by itself on a single-pick grid with manual
    confirmation off, so `confirm_selection` afterward is best-effort (`_try`), same as
    every other multi-action branch here (`rest_site`, `treasure`)."""
    cards = (state.get("card_select") or {}).get("cards") or []
    if not cards:
        return
    index = cards[0].get("index", 0)
    if _try(client, {"action": "select_card", "index": index}) is not None:
        _try(client, {"action": "confirm_selection"})


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
    stable_reads: int = 2,
) -> dict:
    """Poll until `predicate` holds for `stable_reads` consecutive reads, or return the
    last state read once `timeout_s` runs out. Confirmed live: a menu/room transition
    can flicker through a state that passes the predicate for one read and then does
    not (a card_reward-turned-map read here, a map-vs-room read there), the same reason
    `coach.bridge.decisions.Stabilizer` waits for two settled combat reads before
    trusting one; this is that idea applied to the menu/room side."""
    deadline = time.monotonic() + timeout_s
    hits = 0
    state = client.get_state()
    while True:
        hits = hits + 1 if predicate(state) else 0
        if hits >= stable_reads:
            return state
        if time.monotonic() > deadline:
            return state
        time.sleep(poll_s)
        state = client.get_state()
    return state
