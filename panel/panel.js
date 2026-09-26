// The coach panel. It renders each CoachState the coach pushes over /ws; the coach
// sends the current state as soon as the socket opens, so a reconnect shows it at once.
// While the socket is down, the last state stays on screen, dimmed, and the panel
// tries again with a growing delay. Every problem is one sentence on the status line.

"use strict";

(() => {
  const RETRY_MS = [500, 1000, 2000, 4000, 8000]; // then every 8 s
  const NOTICE_MS = 6000; // how long the panel's own error stays on the status line
  const MODE_WAIT_MS = 3000; // how long a mode switch may take to show up in a state
  const THEN_COMPACT_MAX = 3; // steps of the rest of the turn shown in the compact layout
  const WARNINGS_COMPACT_MAX = 1;
  const LAYOUT_KEY = "spire-coach.layout";

  const PLACEHOLDERS = {
    connecting: "Connecting…",
    waiting_for_game: "Waiting for the game",
    idle: "Nothing to decide",
    thinking: "Thinking…",
    engine_restarting: "Restarting…",
    advice_off: "Advice is off",
  };
  const GRADES = ["Best", "Good", "Inaccuracy", "Mistake", "Blunder", "Ungraded"];
  // A move's identity; its label is only a description.
  const MOVE_FIELDS = [
    "kind", "card_index", "card_uid", "card_id", "potion_slot", "potion_id", "target",
    "select_index",
  ];

  const byId = (id) => document.getElementById(id);
  const view = {
    run: byId("run"),
    modes: Array.from(document.querySelectorAll("[data-mode]")),
    layout: byId("layout"),
    statusText: byId("status-text"),
    override: byId("override"),
    last: byId("last"),
    chip: byId("chip"),
    gradeUnreliable: byId("grade-unreliable"),
    lastPlayed: byId("last-played"),
    lastDetail: byId("last-detail"),
    unreliable: byId("unreliable"),
    best: byId("best"),
    bestName: byId("best-name"),
    bestTarget: byId("best-target"),
    then: byId("then"),
    path: byId("path"),
    outcome: byId("outcome"),
    hpLoss: byId("hp-loss"),
    deathRisk: byId("death-risk"),
    warnings: byId("warnings"),
    others: byId("others"),
    alternatives: byId("alternatives"),
    strategy: byId("strategy"),
    strategyScreen: byId("strategy-screen"),
    strategyBestName: byId("strategy-best-name"),
    strategyOutcome: byId("strategy-outcome"),
    strategyBossWin: byId("strategy-boss-win"),
    strategyOptions: byId("strategy-options"),
  };

  let state = null; // the last CoachState from the coach
  let connected = false;
  let socket = null; // the open or opening socket; null while waiting to retry
  let attempt = 0;
  let retryTimer = null;
  let pendingMode = null; // a mode asked for that no state has shown yet
  let notice = null; // {text, until}: the panel's own last error
  let shownGrade = null; // the last grade shown, to mark a new one

  // --- the connection -----------------------------------------------------------------

  function connect() {
    retryTimer = null;
    const scheme = location.protocol === "https:" ? "wss://" : "ws://";
    let ws;
    try {
      ws = new WebSocket(scheme + location.host + "/ws");
    } catch {
      retryLater();
      return;
    }
    socket = ws;
    ws.onopen = () => {
      attempt = 0;
    };
    ws.onmessage = (event) => {
      let next;
      try {
        next = JSON.parse(event.data);
      } catch {
        return; // not a state: nothing to show
      }
      state = next;
      connected = true;
      if (pendingMode !== null && state.mode === pendingMode) pendingMode = null;
      render();
    };
    ws.onclose = () => {
      if (socket !== ws) return;
      socket = null;
      connected = false;
      render();
      retryLater();
    };
  }

  function retryLater() {
    const base = RETRY_MS[Math.min(attempt, RETRY_MS.length - 1)];
    attempt += 1;
    retryTimer = setTimeout(connect, base * (0.8 + Math.random() * 0.4));
  }

  function retryNow() {
    if (socket !== null) return;
    clearTimeout(retryTimer);
    attempt = 0;
    connect();
  }

  // --- settings -----------------------------------------------------------------------

  async function sendSettings(update) {
    let response;
    try {
      response = await fetch("/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(update),
      });
    } catch {
      showNotice("Could not reach the coach to change the setting.");
      return false;
    }
    if (response.ok) return true;
    let text = "The coach did not accept the setting.";
    try {
      const body = await response.json();
      if (body && typeof body.error === "string" && body.error) text = body.error;
    } catch {
      // keep the general sentence
    }
    showNotice(text);
    return false;
  }

  async function chooseMode(mode) {
    if (state === null || mode === shownMode()) return;
    pendingMode = mode;
    render();
    if (await sendSettings({ mode })) {
      setTimeout(() => {
        if (pendingMode === mode) {
          pendingMode = null; // the coach never showed it: show what it has
          render();
        }
      }, MODE_WAIT_MS);
    } else {
      pendingMode = null;
      render();
    }
  }

  function showNotice(text) {
    notice = { text, until: Date.now() + NOTICE_MS };
    render();
    setTimeout(render, NOTICE_MS + 50);
  }

  function shownMode() {
    return pendingMode ?? (state ? state.mode : "show");
  }

  function setLayout(layout) {
    document.body.dataset.layout = layout;
    view.layout.textContent = layout === "compact" ? "Expand" : "Compact";
    view.layout.title =
      layout === "compact" ? "Show the other moves and details" : "Show only the essentials";
    try {
      localStorage.setItem(LAYOUT_KEY, layout);
    } catch {
      // storage can be unavailable: the layout just resets next time
    }
    render();
  }

  // --- rendering ----------------------------------------------------------------------

  function render() {
    try {
      renderAll();
    } catch {
      view.statusText.textContent = "The panel could not show the coach's latest update.";
      document.body.dataset.tone = "error";
    }
  }

  function renderAll() {
    document.body.classList.toggle("stale", state !== null && !connected);
    renderBar();
    renderStatus();
    renderGrade(state ? state.last_grade : null);
    renderAdvice(state && state.recommendation ? state.recommendation : null);
    renderStrategy(state && state.strategy ? state.strategy : null);
  }

  function renderBar() {
    const run = state && state.run ? state.run : {};
    view.run.textContent =
      Number.isInteger(run.act) && Number.isInteger(run.floor)
        ? `Act ${run.act}, floor ${run.floor}`
        : "";
    const mode = shownMode();
    for (const button of view.modes) {
      button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
      button.disabled = !connected;
    }
  }

  function renderStatus() {
    let tone;
    let text;
    if (!connected) {
      tone = state ? "lost" : "connecting";
      text = state ? "Lost contact with the coach. Reconnecting…" : "Connecting to the coach…";
    } else if (notice && Date.now() < notice.until) {
      tone = "error";
      text = notice.text;
    } else {
      tone = cautious(state) ? "advice-caution" : state.status;
      text = state.status_text;
    }
    document.body.dataset.tone = tone;
    view.statusText.textContent = text;

    const action = connected ? overrideAction(state) : null;
    view.override.hidden = action === null;
    if (action !== null) {
      view.override.textContent = action.label;
      view.override.dataset.value = String(action.value);
    }
  }

  // Advice to take with care: the engine calls it unreliable, or it only shows because
  // the version check is overridden.
  function cautious(current) {
    if (current.status !== "advice") return false;
    const rec = current.recommendation;
    return (rec && rec.reliable === false) || (current.versions && current.versions.ok === false);
  }

  // The version override: offered when the version check turned advice off, and to
  // turn off again while advice shows only because of it.
  function overrideAction(current) {
    if (!current || !current.versions || current.versions.ok) return null;
    if (current.status === "advice_off") return { label: "Show advice anyway", value: true };
    if (["advice", "thinking", "engine_restarting"].includes(current.status)) {
      return { label: "Turn off override", value: false };
    }
    return null;
  }

  function renderGrade(grade) {
    view.last.hidden = !grade;
    if (!grade) {
      shownGrade = null;
      return;
    }
    const label = GRADES.includes(grade.label) ? grade.label : "Ungraded";
    view.chip.textContent = label;
    view.chip.dataset.grade = label.toLowerCase();
    view.gradeUnreliable.hidden = !grade.unreliable;
    view.lastPlayed.textContent = moveLabel(grade.played);
    fill(view.lastDetail, gradeDetail(grade, label));

    const key = JSON.stringify(grade);
    if (shownGrade !== null && key !== shownGrade) {
      view.chip.classList.remove("fresh");
      void view.chip.offsetWidth; // restart the animation
      view.chip.classList.add("fresh");
    }
    shownGrade = key;
  }

  function gradeDetail(grade, label) {
    const played = moveLabel(grade.played);
    const best = grade.best ? moveLabel(grade.best) : null;
    if (label === "Best") {
      const same = grade.best && grade.played && sameMove(grade.best, grade.played);
      return ["You played ", move(played), same ? ", the best move." : ", as good as the best."];
    }
    if (label === "Ungraded") {
      return [
        "You played ",
        move(played),
        best
          ? ". There was too little data to grade it."
          : ". The coach had no analysis of that position.",
      ];
    }
    const parts = ["You played ", move(played), "."];
    if (best) {
      const cost = hp(grade.cost_mean_hp);
      parts.push(" ", move(best), cost === null ? " was better." : ` was about ${cost} HP better.`);
    }
    const risk = grade.death_risk_delta_lower;
    if (typeof risk === "number" && risk >= 0.1) {
      parts.push(` It raised the death risk by at least ${Math.round(risk * 100)}%.`);
    }
    return parts;
  }

  function renderAdvice(rec) {
    const hidden = rec !== null && rec.best === null; // quiz mode, before your move
    view.unreliable.hidden = !(rec && rec.reliable === false);

    view.best.classList.toggle("placeholder", rec === null || hidden);
    if (rec === null) {
      const status = state ? state.status : "connecting";
      view.bestName.textContent = PLACEHOLDERS[status] || "No advice";
      view.bestTarget.textContent = "";
    } else if (hidden) {
      view.bestName.textContent = "Hidden until you move";
      view.bestTarget.textContent = "";
    } else {
      const [name, target] = splitLabel(moveLabel(rec.best));
      view.bestName.textContent = name;
      view.bestTarget.textContent = target;
    }

    renderPath(rec && !hidden ? restOfTurn(rec) : []);
    renderOutcome(rec);
    renderWarnings(rec ? rec.warnings : null);
    renderAlternatives(rec && !hidden ? rec.alternatives : null);
  }

  function renderPath(steps) {
    view.then.hidden = steps.length === 0;
    view.path.replaceChildren(...shortList(steps.map(moveLabel), THEN_COMPACT_MAX, "moves"));
  }

  function renderOutcome(rec) {
    view.outcome.hidden = rec === null;
    if (rec === null) return;
    const loss = hp(rec.expected_hp_loss);
    view.hpLoss.textContent = loss === null ? "unknown" : loss;
    view.deathRisk.textContent = percent(rec.p_death) ?? "unknown";
    view.deathRisk.dataset.level = riskLevel(rec.p_death);
  }

  function renderWarnings(warnings) {
    const list = Array.isArray(warnings) ? warnings.filter((w) => typeof w === "string") : [];
    view.warnings.hidden = list.length === 0;
    view.warnings.replaceChildren(...shortList(list, WARNINGS_COMPACT_MAX, "warnings"));
  }

  function renderAlternatives(alternatives) {
    const list = Array.isArray(alternatives) ? alternatives : [];
    view.others.hidden = list.length === 0;
    view.alternatives.replaceChildren(
      ...list.map((alt) => {
        const row = document.createElement("li");
        const name = document.createElement("span");
        name.className = "move";
        name.textContent = moveLabel(alt.move);
        const cost = document.createElement("span");
        cost.className = "cost";
        cost.textContent = hp(alt.cost_mean_hp) ?? "no data";
        const lower = hp(alt.cost_lower_hp);
        if (lower !== null) cost.title = `At least ${lower} HP, 90% sure`;
        row.append(name, cost);
        const risk = alt.p_death_delta;
        if (typeof risk === "number" && risk >= 0.01) {
          const extra = document.createElement("span");
          extra.className = "risk";
          extra.textContent = `death risk up ${percent(risk)}`;
          row.append(extra);
        }
        return row;
      }),
    );
  }

  // Non-combat advice: the strategy champion's ranked options for the screen on show
  // (a map, a card reward, a shop, ...), independent of the combat advice above.
  function renderStrategy(strategy) {
    const options = strategy && Array.isArray(strategy.options) ? strategy.options : null;
    view.strategy.hidden = !options || options.length === 0;
    if (!options || options.length === 0) return;

    view.strategyScreen.textContent = strategy.screen || "";
    view.strategyScreen.hidden = !strategy.screen;
    view.strategyBestName.textContent = options[0].label || "Option";

    const chance = percent(strategy.boss_win_chance);
    view.strategyOutcome.hidden = chance === null;
    view.strategyBossWin.textContent = chance ?? "";

    view.strategyOptions.replaceChildren(
      ...options.map((option, index) => {
        const row = document.createElement("li");
        const name = document.createElement("span");
        name.className = "move";
        name.textContent = option.label || "Option";
        const gap = document.createElement("span");
        gap.className = "cost";
        gap.textContent = index === 0 ? "best" : (valueGap(option.gap) ?? "no data");
        row.append(name, gap);
        return row;
      }),
    );
  }

  // The value's gap to the best option, in the champion's own units (expected final
  // floor, DESIGN.md 5.2): not HP, so shown with two decimals rather than `hp()`'s.
  function valueGap(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return null;
    return `−${value.toFixed(2)}`;
  }

  // --- helpers ------------------------------------------------------------------------

  function moveLabel(value) {
    return value && typeof value.label === "string" && value.label ? value.label : "a move";
  }

  function sameMove(a, b) {
    return MOVE_FIELDS.every((field) => a[field] === b[field]);
  }

  // The engine's line starts with the best move itself; the rest is what follows it.
  function restOfTurn(rec) {
    const line = Array.isArray(rec.line) ? rec.line : [];
    const skip = line.length > 0 && rec.best && sameMove(line[0], rec.best) ? 1 : 0;
    return line.slice(skip);
  }

  // "Bash → Jaw Worm" is shown as the card, then its target on a line of its own.
  function splitLabel(label) {
    const at = label.indexOf(" → ");
    return at < 0 ? [label, ""] : [label.slice(0, at), label.slice(at + 1)];
  }

  function hp(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return null;
    const size = Math.abs(value);
    if (size < 0.05) return "0";
    const text = size >= 10 ? String(Math.round(value)) : String(Number(value.toFixed(1)));
    return text.replace("-", "−");
  }

  function percent(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return null;
    if (value <= 0) return "0%";
    if (value < 0.01) return "<1%";
    if (value > 0.99 && value < 1) return ">99%";
    return `${Math.round(value * 100)}%`;
  }

  function riskLevel(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return "";
    if (value >= 0.1) return "danger";
    if (value >= 0.01) return "caution";
    return "";
  }

  function move(label) {
    return { move: label };
  }

  // Fill `element` with text and move names (in the game's typeface).
  function fill(element, parts) {
    element.replaceChildren(
      ...parts.map((part) => {
        if (typeof part === "string") return document.createTextNode(part);
        const span = document.createElement("span");
        span.className = "move";
        span.textContent = part.move;
        return span;
      }),
    );
  }

  // List items for `texts`. The compact layout shows only the first `max`, then "and
  // N more <things>", but only when that saves a line: "and 1 more" would take the
  // hidden item's own line, so that item is shown instead.
  function shortList(texts, max, things) {
    const cut = document.body.dataset.layout === "compact" && texts.length > max + 1;
    const items = (cut ? texts.slice(0, max) : texts).map((text) => item(text));
    if (cut) items.push(item(`and ${texts.length - max} more ${things}`, "more"));
    return items;
  }

  function item(text, className) {
    const li = document.createElement("li");
    li.textContent = text;
    if (className) li.className = className;
    return li;
  }

  // --- start --------------------------------------------------------------------------

  for (const button of view.modes) {
    button.addEventListener("click", () => chooseMode(button.dataset.mode));
  }
  view.override.addEventListener("click", () => {
    sendSettings({ override_versions: view.override.dataset.value === "true" });
  });
  view.layout.addEventListener("click", () => {
    setLayout(document.body.dataset.layout === "compact" ? "expanded" : "compact");
  });
  // After sleep or a network change, try again at once rather than after the delay.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) retryNow();
  });
  window.addEventListener("online", retryNow);

  let saved = null;
  try {
    saved = localStorage.getItem(LAYOUT_KEY);
  } catch {
    // no storage: start expanded
  }
  setLayout(saved === "compact" ? "compact" : "expanded");
  connect();
})();
