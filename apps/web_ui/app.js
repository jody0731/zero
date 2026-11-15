/* ZERO's web UI: the sessions, one session's timeline, and its details,
 * all from ZERO's server through the HTTP door; the page keeps nothing of its
 * own but browser pairing, unsent drafts, appearance and panel widths.
 *
 * Every request is one protocol message, whole (POST /api/request); one
 * stream (GET /api/events) brings the session list and the open session's
 * events after the last seq the page holds. A message that changes something
 * carries an id the page picks, sent again unchanged on a retry, so it runs
 * once. The modes, an ask's choices and their words come from the server.
 * The page builds every node with textContent: nothing a session holds is
 * read as HTML.
 */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  // Records the page doesn't draw: what the model was given, step marks
  const UNDRAWN = new Set(["system", "step"]);
  // A pause between two entries longer than this shows as a gap
  const GAP_SECONDS = 10 * 60;
  // A hidden page lets go of its stream after this long
  const HIDDEN_SECONDS = 60;
  const PAGE_RECORDS = 150;
  const RESULT_CHARS = 20000;
  const WIDTHS = { left: [245, 200, 460], right: [285, 260, 640] };

  const state = {
    hello: null, sessions: [], open: null, records: [], earlier: 0,
    runsWith: null, context: null, events: null, asleep: false,
    calls: new Map(), lastSeq: 0, lastTime: null, draft: null,
    hiddenTimer: null, seenSent: 0, turn: null, archived: false,
    stoppingJobs: new Set(), detailsVersion: 0,
    settings: [], models: [], commandIndex: 0, launch: {},
    expandedJobs: new Map(), expandedFamilies: new Map(),
    settingsDraft: new Map(), settingsErrors: new Map(), settingsSaving: false,
    settingsView: "General", modelLookup: null,
    rewindPoints: new Set(), composerDrafts: new Map(),
    jobs: new Map(), settingsDirty: false, features: new Map(), turnStarted: null,
  };

  // ------------------------------------------------------------ requests

  function newId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return String(Date.now()) + Math.random().toString(16).slice(2);
  }

  async function post(payload, restored = false) {
    const reply = await fetch("/api/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      credentials: "same-origin",
    });
    if (reply.status === 401) {
      if (!restored) {
        await pairBrowser();
        return post(payload, true);
      }
      const error = new Error("This browser is not paired. Run zero web once.");
      error.status = 401;
      throw error;
    }
    const body = await reply.json();
    if (!reply.ok) throw new Error(body.error || `HTTP ${reply.status}`);
    return body;
  }

  // A look: its reply, an error thrown for a no
  async function look(message, fields) {
    const reply = await post({ message, ...(fields || {}) });
    if (!reply.ok) throw new Error(reply.error);
    return reply;
  }

  // A change: its own id, sent again with the same id if the first try
  // never came back, so the server runs it once
  async function change(message, fields) {
    const payload = { message, id: newId(), ...(fields || {}) };
    let reply;
    try {
      reply = await post(payload);
    } catch (error) {
      if (error.status === 401) throw error;
      reply = await post(payload);
    }
    wake();
    if (!reply.ok) throw new Error(reply.error);
    return reply;
  }

  // ------------------------------------------------------------ helpers

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function folderName(path) {
    if (!path || path === "/") return "/";
    const parts = path.replace(/\/+$/, "").split("/");
    return parts[parts.length - 1] || path;
  }

  function seconds(value) {
    if (value === null || value === undefined) return "";
    const s = Number(value);
    if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
    if (s < 3600) return `${Math.round(s / 60)} min`;
    return `${(s / 3600).toFixed(1)} h`;
  }

  function clock(iso) {
    const when = new Date(iso);
    if (Number.isNaN(when.getTime())) return "";
    return when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function stateWord(row) {
    if (row.ask || row.state === "waiting") return "waiting";
    if (row.state === "running") return "running";
    if (row.state === "failed") return "failed";
    return row.unseen ? "news" : "idle";
  }

  function sourceWho(source) {
    if (!source || source === "web" || String(source).startsWith("terminal")) {
      return "You";
    }
    if (source === "mcp") return "Agent";
    if (source === "zero" || source === "subagent") return "ZERO";
    return String(source);
  }

  function modeAbout(name) {
    const mode = (state.hello && state.hello.modes || []).find(
      (each) => each.name === name);
    return mode ? mode.about : "";
  }

  // ------------------------------------------------------------ stream

  function connect() {
    if (state.events) state.events.close();
    const query = state.open
      ? `?session=${encodeURIComponent(state.open)}&after=${state.lastSeq}`
      : "";
    const events = new EventSource(`/api/events${query}`);
    state.events = events;
    events.addEventListener("list", (event) => {
      const data = JSON.parse(event.data);
      if (data.sessions) {
        state.sessions = data.sessions;
        drawSessions();
        drawHead();
        drawAsk();
        drawDetails();
        // What arrives while the page is looked at is seen
        if (!document.hidden) markSeen();
      }
    });
    events.addEventListener("session", (event) => {
      if (state.events === events) sessionEvent(JSON.parse(event.data));
    });
    events.addEventListener("asleep", () => {
      state.asleep = true;
      drawServer();
    });
    events.addEventListener("awake", () => {
      state.asleep = false;
      drawServer();
    });
    events.onopen = () => {
      state.asleep = false;
      drawServer();
    };
  }

  function wake() {
    if (state.asleep || !state.events) connect();
  }

  function sessionEvent(event) {
    // A session deleted, or one the server doesn't have
    if (event.bye === "deleted" || event.bye === "refused") {
      closeSession();
      return;
    }
    if (event.record) {
      addRecord(event.record);
      return;
    }
    if (event.draft) {
      drawDraft(event.draft);
      return;
    }
    if (event.context !== undefined) {
      state.context = event.context;
      drawDetails();
      return;
    }
    if (event.settled) {
      clearDraft();
      markSeen();
      const session = state.open;
      look("session/user_messages", { session }).then((reply) => {
        if (state.open !== session) return;
        state.rewindPoints = new Set(reply.messages.map((item) => item.seq));
        for (const node of $("timeline").querySelectorAll(".entry.you")) {
          const record = state.records.find((item) => item.seq === +node.dataset.seq);
          if (record) node.replaceWith(userEntry(record));
        }
      }).catch(() => {});
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      state.hiddenTimer = setTimeout(() => {
        if (state.events) state.events.close();
        state.events = null;
      }, HIDDEN_SECONDS * 1000);
      return;
    }
    clearTimeout(state.hiddenTimer);
    if (!state.events) connect();
  });

  // ------------------------------------------------------------ sessions

  function drawSessions() {
    const list = $("session-list");
    const editing = new Map(Array.from(list.querySelectorAll(".row")).filter((item) =>
      item.contains(document.activeElement) || item.querySelector("details[open]"))
      .map((item) => [item.dataset.session, item]));
    const rows = state.sessions.filter(
      (row) => Boolean(row.archived) === state.archived).sort(
      (a, b) => (b.updated_at || 0) - (a.updated_at || 0));
    const ids = new Set(rows.map((row) => row.id));
    const children = new Map();
    for (const row of rows) {
      if (row.parent && ids.has(row.parent)) {
        if (!children.has(row.parent)) children.set(row.parent, []);
        children.get(row.parent).push(row);
      }
    }
    // The ones that wait for you first, then the rest by their last change
    const tops = rows.filter((row) => !row.parent || !ids.has(row.parent));
    tops.sort((a, b) => (stateWord(b) === "waiting") - (stateWord(a) === "waiting"));
    list.replaceChildren();
    const seen = new Set();
    for (const row of tops) {
      const branch = sessionBranch(row, children, seen, false, editing);
      if (branch) list.append(branch);
    }
    if (!list.children.length) {
      list.append(el("li", "loading", "—"));
    }
  }

  function sessionRow(row, kids, family = false) {
    const item = el("div", "row" + (family ? " family-row tree-session" : "")
      + (row.id === state.open ? " open" : ""));
    item.tabIndex = 0;
    item.title = `${row.title || "Untitled"} · ${row.workspace || ""}`;
    if (family) item.dataset.familySession = row.id;
    else item.dataset.session = row.id;
    if (row.id === state.open) item.setAttribute("aria-current", "true");
    const dot = el("span", "dot" + (row.unseen ? " unseen" : ""));
    const title = el("span", "title tree-title", row.title || "(untitled)");
    const word = stateWord(row);
    const badges = [];
    if (row.jobs) badges.push(`${row.jobs} job${row.jobs > 1 ? "s" : ""}`);
    if (row.rounds) badges.push("rounds");
    if (kids) badges.push(`+${kids}`);
    // Only what needs an eye: the square already marks news
    const status = el("span", `state ${word}`,
      ["running", "waiting", "failed"].includes(word) ? word : "");
    const where = el("span", "where", [folderName(row.workspace),
      row.started_by === "zero" ? "subagent" : (row.parent ? "fork" : ""),
      ...badges].filter(Boolean).join(" · "));
    where.title = row.workspace || "";
    item.append(dot, title, status, where);
    if (!family) item.append(sessionActions(row));
    item.addEventListener("click", (event) => {
      if (!event.target.closest("button, input, details")) openSession(row.id);
    });
    item.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && event.target === item) openSession(row.id);
    });
    return item;
  }

  function sessionBranch(row, children, seen, family, editing = new Map()) {
    if (seen.has(row.id)) return null;
    seen.add(row.id);
    const kids = children.get(row.id) || [];
    const branch = el("li", "session-branch");
    const line = editing.get(row.id) || sessionRow(row, kids.length, family);
    line.classList.toggle("open", row.id === state.open);
    if (row.id === state.open) line.setAttribute("aria-current", "true");
    else line.removeAttribute("aria-current");
    branch.append(line);
    if (!kids.length) return branch;
    const nested = el("ul", "session-children");
    nested.dataset.treeParent = row.id;
    for (const kid of kids) {
      const child = sessionBranch(kid, children, seen, family, editing);
      if (child) nested.append(child);
    }
    const toggle = el("button", "tree-toggle");
    toggle.type = "button";
    toggle.dataset.treeToggle = row.id;
    const update = (control, expanded) => {
      control.textContent = expanded ? "▾" : "▸";
      control.setAttribute("aria-expanded", String(expanded));
      control.setAttribute("aria-label",
        `${expanded ? "Collapse" : "Expand"} ${row.title || row.id}`);
    };
    const expanded = state.expandedFamilies.get(row.id) ?? true;
    nested.hidden = !expanded;
    update(toggle, expanded);
    toggle.addEventListener("click", () => {
      const expanded = !(state.expandedFamilies.get(row.id) ?? true);
      state.expandedFamilies.set(row.id, expanded);
      document.querySelectorAll("[data-tree-parent]").forEach((list) => {
        if (list.dataset.treeParent === row.id) list.hidden = !expanded;
      });
      document.querySelectorAll("[data-tree-toggle]").forEach((control) => {
        if (control.dataset.treeToggle === row.id) update(control, expanded);
      });
    });
    line.firstElementChild.replaceWith(toggle);
    branch.append(nested);
    return branch;
  }

  function rowOf(id) {
    return state.sessions.find((row) => row.id === id) || null;
  }

  // ------------------------------------------------------------ a session

  async function openSession(id) {
    const version = (state.openVersion || 0) + 1;
    state.openVersion = version;
    if (state.events) state.events.close();
    state.events = null;
    saveComposerDraft();
    state.open = id;
    state.rewindPoints = new Set();
    restoreComposerDraft();
    history.replaceState(null, "", `#session=${encodeURIComponent(id)}`);
    state.turn = null;
    state.settingsDirty = false;
    state.turnStarted = null;
    state.records = [];
    state.calls = new Map();
    state.lastSeq = 0;
    state.lastTime = null;
    state.draft = null;
    state.seenSent = 0;
    showPanel("session");
    drawSessions();
    drawHead();
    const timeline = $("timeline");
    timeline.replaceChildren(el("div", "loading", "…"));
    try {
      const [reply, points] = await Promise.all([
        look("session/read", { session: id, limit: PAGE_RECORDS }),
        look("session/user_messages", { session: id }),
      ]);
      if (state.open !== id || state.openVersion !== version) return;
      state.rewindPoints = new Set(points.messages.map((item) => item.seq));
      state.earlier = reply.earlier_count;
      state.runsWith = reply.runs_with;
      state.context = reply.context;
      timeline.replaceChildren();
      if (state.earlier) timeline.append(earlierButton());
      for (const record of reply.records) addRecord(record, false);
      scrollToEnd();
    } catch (error) {
      if (state.openVersion !== version) return;
      timeline.replaceChildren(el("div", "loading", error.message));
    }
    drawDetails();
    drawAsk();
    connect();
    markSeen();
  }

  function closeSession() {
    state.openVersion = (state.openVersion || 0) + 1;
    saveComposerDraft();
    state.open = null;
    restoreComposerDraft();
    $("session-title").textContent = "";
    $("timeline").replaceChildren();
    showPanel("sessions");
    drawAsk();
    drawDetails();
    connect();
  }

  function earlierButton() {
    const button = el("button", "earlier ghost",
      `Earlier · ${state.earlier}`);
    button.type = "button";
    button.addEventListener("click", loadEarlier);
    return button;
  }

  async function loadEarlier() {
    const first = state.records.length ? state.records[0].seq : null;
    if (first === null) return;
    const id = state.open;
    const reply = await look("session/read", { session: id, before: first,
                                               limit: PAGE_RECORDS });
    if (state.open !== id) return;
    const expanded = new Set(Array.from($("timeline").querySelectorAll(
      ".process[open] [data-seq]")).map((node) => node.dataset.seq));
    const folds = new Set(Array.from($("timeline").querySelectorAll(
      "details[open][data-fold]")).map((node) => node.dataset.fold));
    const calls = new Set(Array.from($("timeline").querySelectorAll(
      "details[open][data-call]")).map((node) => node.dataset.call));
    // Drawn again whole, the earlier ones first
    const records = reply.records.concat(state.records);
    state.earlier = reply.earlier_count;
    state.records = [];
    state.calls = new Map();
    state.lastTime = null;
    state.lastSeq = 0;
    state.turn = null;
    const timeline = $("timeline");
    const height = timeline.scrollHeight;
    timeline.replaceChildren();
    if (state.earlier) timeline.append(earlierButton());
    for (const record of records) addRecord(record, false);
    for (const process of timeline.querySelectorAll(".process")) {
      process.open = Array.from(process.querySelectorAll("[data-seq]"))
        .some((node) => expanded.has(node.dataset.seq));
    }
    for (const fold of timeline.querySelectorAll("details")) {
      if (folds.has(fold.dataset.fold) || calls.has(fold.dataset.call)) {
        fold.open = true;
      }
    }
    timeline.scrollTop = timeline.scrollHeight - height;
  }

  function atEnd() {
    const timeline = $("timeline");
    return timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 80;
  }

  function scrollToEnd() {
    const timeline = $("timeline");
    timeline.scrollTop = timeline.scrollHeight;
    $("follow").hidden = true;
  }

  $("timeline").addEventListener("scroll", () => {
    if (atEnd()) $("follow").hidden = true;
  });
  $("follow").addEventListener("click", scrollToEnd);

  function drawHead() {
    const row = rowOf(state.open);
    $("session-title").textContent = row ? row.title || row.id : state.open || "";
  }

  async function markSeen() {
    const row = rowOf(state.open);
    if (!row || !state.lastSeq || state.lastSeq <= state.seenSent) return;
    if (!row.unseen) return;
    state.seenSent = state.lastSeq;
    try {
      await change("session/mark_seen", { session: state.open,
                                          seq: state.lastSeq });
    } catch (error) {
      state.seenSent = 0;
    }
  }

  // ------------------------------------------------------------ the timeline

  function addRecord(record, live = true) {
    if (record.seq <= state.lastSeq) return;
    const following = live && atEnd();
    state.lastSeq = record.seq;
    state.records.push(record);
    if (UNDRAWN.has(record.type) || record.audience === "model") return;
    if (record.type === "user" && !record.steer) {
      state.turn = null;
      state.turnStarted = record.time;
    }
    clearDraft();
    const timeline = $("timeline");
    if (record.time) {
      const at = new Date(record.time).getTime() / 1000;
      if (!state.turn && state.lastTime !== null
          && at - state.lastTime > GAP_SECONDS) {
        timeline.append(el("div", "divider",
          `${seconds(at - state.lastTime)} later`));
      }
      state.lastTime = at;
    }
    const node = entryFor(record);
    if (node) {
      if (record.type === "user" && !record.notice && !record.steer) {
        timeline.append(node);
      } else if (record.type === "answer" || record.type === "user") {
        const turn = turnFor(record);
        (record.notice ? turn.process : turn.replies).append(node);
      } else {
        timeline.append(node);
      }
    }
    if (following) scrollToEnd();
    else if (live) $("follow").hidden = false;
  }

  function who(name, record, more) {
    const line = el("div", "who");
    line.append(el("b", null, name));
    const time = record && record.time ? clock(record.time) : "";
    const rest = [time, more].filter(Boolean).join(" · ");
    if (rest) line.append(document.createTextNode(` · ${rest}`));
    return line;
  }

  function entryFor(record) {
    switch (record.type) {
      case "user": return userEntry(record);
      case "assistant": return assistantEntry(record);
      case "tool": return toolEntry(record);
      case "ask": return askEntry(record);
      case "answer": return answerEntry(record);
      case "turn_end": return turnEnd(record);
      case "note": return noteEntry(record);
      case "compaction":
        return el("div", "divider", `Compacted ${tokens(record.tokens_before)}`
          + ` → ${tokens(record.tokens_after)}`);
      case "compaction_failed":
        return el("div", "divider", "Compaction failed");
      case "nothing_to_compact": return null;
      case "rewind": return el("div", "divider", "Rewound");
      case "switch": return el("div", "divider", switchText(record));
      case "session":
        return el("div", "divider", record.note || "Started");
      default: return null;
    }
  }

  function tokens(count) {
    const n = Number(count) || 0;
    return n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
  }

  function switchText(record) {
    const to = record.to || {};
    const parts = Object.keys(to).map((name) => `${name} ${to[name]}`);
    return parts.length ? parts.join(" · ") : "Switched";
  }

  function userEntry(record) {
    const node = el("div", "entry you");
    node.dataset.seq = record.seq;
    if (record.notice) {
      node.className = "entry notice";
      const name = record.notice_source || "Background notice";
      node.append(who(name, record));
      node.append(el("div", "bubble", record.text || ""));
      if (record.reference) {
        const link = el("button", null, "Show job");
        link.addEventListener("click", () => {
          showPanel("details");
          const view = Array.from(document.querySelectorAll("[data-job]"))
            .find((item) => item.dataset.job === record.reference);
          if (view) { view.open = true; view.scrollIntoView(); }
        });
        node.append(link);
      }
      return node;
    }
    const more = record.steer ? "steer" : (record.continue ? "go on" : "");
    node.append(who(sourceWho(record.source), record, more));
    if (record.continue && !record.text) return node;
    node.append(el("div", "bubble", record.text || ""));
    if (state.rewindPoints.has(record.seq)) {
      const session = state.open;
      const button = el("button", "message-action", "Rewind and edit");
      button.type = "button";
      button.addEventListener("click", () => rewindMessage(session, record.seq));
      node.append(button);
    }
    return node;
  }

  function turnFor(record) {
    if (state.turn) return state.turn;
    const node = el("article", "entry zero turn");
    node.append(who("ZERO", { time: state.turnStarted || record.time },
      record.notice ? `Following up on ${record.notice_source}` : ""));
    const process = el("details", "process");
    const summary = el("summary", null, "Thinking…");
    process.append(summary);
    const replies = el("div", "turn-replies");
    const copy = el("button", "message-action", "Copy response");
    copy.type = "button";
    copy.hidden = true;
    const texts = [];
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(texts.join("\n\n"));
        copy.textContent = "Copied";
        setTimeout(() => { copy.textContent = "Copy response"; }, 1800);
      } catch (error) { alert(`Could not copy: ${error.message}`); }
    });
    node.append(process, replies, copy);
    $("timeline").append(node);
    state.turn = { node, process, summary, replies, copy, texts };
    return state.turn;
  }

  function assistantEntry(record) {
    const message = record.message || {};
    const turn = turnFor(record);
    const node = el("div", "process-step");
    node.dataset.seq = record.seq;
    turn.process.append(node);
    if (message.reasoning_content) {
      const fold = el("details", "thinking");
      fold.dataset.fold = `thinking-${record.seq}`;
      fold.append(el("summary", null, "Thinking"),
                  el("div", "body", message.reasoning_content));
      node.append(fold);
    }
    if (message.content) {
      turn.replies.append(markdown(message.content));
      turn.texts.push(message.content);
      turn.copy.hidden = false;
    }
    const summaries = record.call_summaries || {};
    for (const call of message.tool_calls || []) {
      const fn = call.function || {};
      const line = summaries[call.id] || `${fn.name}(${fn.arguments || ""})`;
      const fold = el("details", "call");
      fold.append(el("summary", null, line));
      fold.dataset.call = call.id;
      state.calls.set(call.id, fold);
      node.append(fold);
      turn.summary.textContent = line;
    }
    if (!message.content && !(message.tool_calls || []).length
        && !message.reasoning_content) return null;
    return null;
  }

  function toolResult(fold, record) {
    const summary = fold.querySelector("summary");
    const job = record.fields && record.fields.job_id;
    if (job) {
      const view = jobView({ id: job, command: summary.textContent,
        state: "running", state_text: "Running", exit_code: null });
      view.dataset.job = job;
      fold.append(view);
    }
    for (const [flag, word] of [["denied", "refused"], ["failed", "failed"],
                                ["bad_call", "bad call"]]) {
      if (record[flag]) summary.append(el("span", `tag ${flag === "bad_call"
        ? "bad" : flag}`, word));
    }
    const text = String(record.result || "");
    fold.append(el("pre", null, text.length > RESULT_CHARS
      ? text.slice(0, RESULT_CHARS) + `\n… +${text.length - RESULT_CHARS}`
      : text));
    if (record.saved_path) {
      fold.append(el("div", "saved", record.saved_path));
    }
  }

  function toolEntry(record) {
    const fold = state.calls.get(record.call);
    if (fold) {
      toolResult(fold, record);
      return null;
    }
    // Its call came before the records on the page
    const node = el("div", "process-step");
    const alone = el("details", "call");
    alone.append(el("summary", null, `${record.tool} ${record.arguments || ""}`));
    toolResult(alone, record);
    node.append(alone);
    turnFor(record).process.append(node);
    return null;
  }

  function askEntry(record) {
    const fold = state.calls.get(record.call);
    if (fold) {
      fold.querySelector("summary").append(el("span", "tag asked", "asked"));
      return null;
    }
    const node = el("div", "notice");
    node.textContent = `Asked · ${record.command || record.tool || ""}`;
    return node;
  }

  function answerEntry(record) {
    const node = el("div", "entry you");
    node.append(who(sourceWho(record.source), record, "answered"));
    const words = record.choice && record.choice !== record.text
      ? `${record.choice}${record.text ? ": " + record.text : ""}`
      : (record.text || (record.yes ? "yes" : "no"));
    node.append(el("div", "bubble", words));
    return node;
  }

  function turnEnd(record) {
    const failed = record.outcome === "failed" || record.error;
    const text = [record.outcome, seconds(record.seconds),
      record.error || record.detail || ""].filter(Boolean).join(" · ");
    const turn = turnFor(record);
    turn.summary.textContent = text || "Completed";
    if (failed) turn.summary.classList.add("failed");
    state.turn = null;
    return null;
  }

  function noteEntry(record) {
    const kind = record.kind || "note";
    const name = kind.startsWith("job") ? "Job"
      : kind.startsWith("round") ? "Rounds"
      : kind.startsWith("subagent") ? "Subagent" : "ZERO";
    const node = el("div", "entry");
    node.append(who(name, record, kind.replace(/_/g, " ")));
    node.append(el("div", "notice", record.text || ""));
    return node;
  }

  function drawDraft(draft) {
    clearDraft();
    const node = el("div", "entry zero draft");
    node.id = "draft";
    if (draft.text) node.append(markdown(draft.text));
    else if (draft.reasoning) node.append(el("div", "sub", "…"));
    if ((draft.calls || []).length) {
      node.append(el("div", "sub", `${draft.calls.join(", ")} …`));
    }
    const following = atEnd();
    const turn = turnFor({});
    turn.summary.textContent = (draft.calls || []).length
      ? draft.calls.join(", ") : "Thinking…";
    (draft.text ? turn.replies : turn.process).append(node);
    if (following) scrollToEnd();
  }

  function clearDraft() {
    const old = $("draft");
    if (old) old.remove();
  }

  // ------------------------------------------------------------ markdown

  function markdown(text) {
    const wrap = el("div", "reply");
    const blocks = typeof ZeroMarkdown !== "undefined"
      ? ZeroMarkdown.blocks(text) : [];
    if (!blocks.length) wrap.append(el("p", null, text));
    for (const block of blocks) wrap.append(blockNode(block));
    return wrap;
  }

  function blockNode(block) {
    if (block.type === "heading") {
      const node = el(`h${Math.min(Math.max(block.level || 1, 1), 6)}`);
      node.append(...spans(block.spans));
      return node;
    }
    if (block.type === "code") {
      const pre = el("pre");
      pre.append(el("code", null, block.text));
      return pre;
    }
    if (block.type === "list") {
      const list = el(block.ordered ? "ol" : "ul");
      for (const item of block.items || []) {
        const li = el("li");
        li.append(...spans(item));
        list.append(li);
      }
      return list;
    }
    if (block.type === "quote") {
      const quote = el("blockquote");
      quote.append(...spans(block.spans));
      return quote;
    }
    if (block.type === "rule") return el("hr");
    if (block.type === "table") {
      const wrap = el("div", "table");
      const table = el("table");
      const head = el("tr");
      for (const cell of block.head) {
        const th = el("th");
        th.append(...spans(cell));
        head.append(th);
      }
      table.append(head);
      for (const row of block.rows) {
        const tr = el("tr");
        for (const cell of row) {
          const td = el("td");
          td.append(...spans(cell));
          tr.append(td);
        }
        table.append(tr);
      }
      wrap.append(table);
      return wrap;
    }
    const p = el("p");
    p.append(...spans(block.spans));
    return p;
  }

  function spans(list) {
    return (list || []).map((span) => {
      if (span.kind === "code") return el("code", null, span.text);
      if (span.kind === "link") {
        const a = el("a");
        a.href = span.href;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        if (span.spans) a.append(...spans(span.spans));
        else a.textContent = span.text || span.href;
        return a;
      }
      if (["strong", "em", "del"].includes(span.kind)) {
        const node = el(span.kind);
        node.append(...spans(span.spans));
        return node;
      }
      return document.createTextNode(span.text || "");
    });
  }

  // ------------------------------------------------------------ the ask

  function drawAsk() {
    const box = $("ask");
    const row = rowOf(state.open);
    const ask = row && row.ask;
    $("composer").hidden = !row || Boolean(ask);
    $("stop").hidden = !(row && (row.state === "running" || row.ask));
    if (!ask) {
      box.hidden = true;
      box.replaceChildren();
      return;
    }
    if (box.dataset.ask === ask.id && !box.hidden) return;
    box.dataset.ask = ask.id;
    box.hidden = false;
    box.replaceChildren();
    const head = el("h2", null, "Ask");
    if (ask.about) head.title = ask.about;
    box.append(head);
    box.append(el("pre", null, ask.command || ask.tool || ""));
    if (ask.reason) box.append(el("div", null, ask.reason));
    const choices = el("div", "choices");
    for (const choice of ask.choices || []) {
      const label = (ask.choice_labels || {})[choice] || choice;
      const button = el("button", choice === (ask.choices || [])[0]
        ? "primary" : "", label);
      button.type = "button";
      button.addEventListener("click", () => answer(ask, { choice }));
      choices.append(button);
    }
    box.append(choices);
    const words = el("form", "words");
    const input = el("input");
    input.placeholder = "Answer";
    input.setAttribute("aria-label", "Answer");
    const send = el("button", null, "Send");
    send.type = "submit";
    words.append(input, send);
    words.addEventListener("submit", (event) => {
      event.preventDefault();
      if (input.value.trim()) answer(ask, { text: input.value.trim() });
    });
    box.append(words);
  }

  async function answer(ask, given) {
    const box = $("ask");
    for (const button of box.querySelectorAll("button")) button.disabled = true;
    try {
      await change("session/send", {
        session: state.open, source: "web", reply_to: ask.id,
        text: given.text || given.choice, choice: given.choice,
      });
    } catch (error) {
      box.append(el("div", "error", error.message));
      for (const button of box.querySelectorAll("button")) button.disabled = false;
    }
  }

  // ------------------------------------------------------------ sending

  async function send(event) {
    if (event) event.preventDefault();
    const text = $("text").value.trim();
    if (await runCommand(text)) return;
    if (!text || !state.open) return;
    const session = state.open;
    const original = $("text").value;
    const button = $("send");
    button.disabled = true;
    try {
      await change("session/send", {
        session, text, source: "web",
        when_busy: $("queue").checked ? "queue" : "steer",
      });
      if (readComposerDraft(session) === original) {
        writeComposerDraft(session, "");
      }
      if (state.open === session && $("text").value === original) {
        $("text").value = "";
        saveComposerDraft();
        sizeComposer();
        $("queue").checked = false;
      }
    } catch (error) {
      alert(error.message);
    } finally {
      button.disabled = false;
    }
  }

  // Unsent text belongs to this browser and session, never to the server.
  function readComposerDraft(session) {
    if (!session) return "";
    if (state.composerDrafts.has(session)) return state.composerDrafts.get(session);
    try { return localStorage.getItem(`zero.draft.${session}`) || ""; }
    catch (error) { return ""; }
  }

  function writeComposerDraft(session, text) {
    if (!session) return true;
    state.composerDrafts.set(session, text);
    try {
      const key = `zero.draft.${session}`;
      if (text) localStorage.setItem(key, text);
      else localStorage.removeItem(key);
      return true;
    } catch (error) { return false; }
  }

  function saveComposerDraft() {
    const text = $("text").value;
    const saved = writeComposerDraft(state.open, text);
    $("draft-status").textContent = !text || saved
      ? "" : "Draft kept in this tab only";
  }

  function restoreComposerDraft() {
    $("text").value = readComposerDraft(state.open);
    saveComposerDraft();
    $("commands").hidden = true;
    sizeComposer();
  }

  async function rewindMessage(session, seq) {
    const viewing = state.open;
    const draft = readComposerDraft(session);
    const warning = "Return to before this message? Later conversation is left "
      + "behind. Files and memory stay unchanged."
      + (draft ? " This replaces your unsent draft." : "");
    if (!confirm(warning)) return false;
    try {
      const result = await change("session/rewind", { session, seq });
      writeComposerDraft(session, result.text || "");
      if (state.open === viewing) {
        // Restore before reopening so the previous composer cannot overwrite it.
        if (state.open === session) restoreComposerDraft();
        await openSession(session);
        $("text").focus();
      }
      return true;
    } catch (error) {
      alert(error.message);
      return false;
    }
  }

  function sizeComposer() {
    const input = $("text");
    const style = getComputedStyle(input);
    const padding = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
    const limit = parseFloat(style.lineHeight) * 10 + padding;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, limit)}px`;
    input.style.overflowY = input.scrollHeight > limit ? "auto" : "hidden";
  }

  $("text").addEventListener("input", () => {
    saveComposerDraft();
    sizeComposer();
    drawCommands();
  });
  window.addEventListener("resize", sizeComposer);
  $("composer").addEventListener("submit", send);
  $("text").addEventListener("keydown", (event) => {
    if (!$("commands").hidden) {
      const choices = $("commands").querySelectorAll("button");
      if (["ArrowDown", "ArrowUp"].includes(event.key)) {
        event.preventDefault();
        state.commandIndex = (state.commandIndex
          + (event.key === "ArrowDown" ? 1 : choices.length - 1)) % choices.length;
        drawCommands(false);
        $("commands").querySelector(".primary")?.scrollIntoView({
          block: "nearest", inline: "nearest",
        });
        return;
      }
      if (event.key === "Escape") {
        $("commands").hidden = true;
        return;
      }
      if (event.key === "Enter" && choices[state.commandIndex]) {
        event.preventDefault();
        choices[state.commandIndex].click();
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      send(event);
    }
  });
  $("stop").addEventListener("click", async () => {
    try {
      await change("session/stop", { session: state.open, source: "web" });
    } catch (error) {
      alert(error.message);
    }
  });

  // ------------------------------------------------------------ details

  async function drawDetails() {
    const panel = $("details");
    const row = rowOf(state.open);
    drawUsage();
    const version = ++state.detailsVersion;
    if (row && (state.settingsDirty || panel.contains(document.activeElement))) {
      const family = familySection(row);
      const oldFamily = panel.querySelector(".session-family");
      if (oldFamily) {
        if (family) oldFamily.replaceWith(family);
        else oldFamily.remove();
      } else if (family) panel.append(family);
      const work = await workSection(row);
      if (version === state.detailsVersion && state.open === row.id) {
        const old = panel.querySelector("#background-jobs");
        if (old) old.replaceWith(work);
      }
      return;
    }
    panel.replaceChildren();
    if (!row) {
      if (state.open) return;
      $("timeline").replaceChildren();
      const welcome = el("div", "empty-session");
      welcome.append(el("h2", null, "Ready when you are."),
        el("p", "sub", "Choose a session or start something new."));
      const button = el("button", "primary", "New session");
      button.addEventListener("click", openNew);
      welcome.append(button);
      $("timeline").append(welcome);
      return;
    }
    const back = el("button", "details-back", "◨");
    back.setAttribute("aria-label", "Collapse side panel");
    back.title = "Collapse side panel";
    back.addEventListener("click", () => {
      $("frame").classList.add("no-details");
      showPanel("session");
    });
    panel.append(back, runsWithSection(row));
    const family = familySection(row);
    if (family) panel.append(family);
    const context = state.context;
    if (context && context.tokens) {
      const section = el("section");
      section.append(el("h2", null, "Context"));
      const line = context.line || null;
      section.append(el("div", null, line
        ? `${tokens(context.tokens)} / ${tokens(line)}`
        : tokens(context.tokens)));
      if (line) {
        const meter = el("div", "meter");
        const fill = el("i");
        fill.style.width = `${Math.min(100, 100 * context.tokens / line)}%`;
        meter.append(fill);
        section.append(meter);
      }
      panel.append(section);
    }
    const work = await workSection(row);
    if (version === state.detailsVersion && state.open === row.id) {
      panel.append(work);
    }
  }

  function fieldInput(setting, value) {
    let input;
    if (["choice", "mode", "model"].includes(setting.kind)) {
      input = el("select");
      const choices = setting.kind === "mode"
        ? (state.hello.modes || []).map((mode) => mode.name)
        : setting.kind === "model"
          ? [...new Set([value, ...state.models].filter(Boolean))]
          : setting.choices;
      if (setting.unset_means) {
        const unset = el("option", null, setting.unset_means);
        unset.value = "";
        input.append(unset);
      }
      for (const choice of choices || []) {
        const option = el("option", null, choice);
        option.value = choice;
        input.append(option);
      }
    } else {
      input = el("input");
      if (["int", "number"].includes(setting.kind)) {
        input.type = "number";
        input.step = setting.kind === "int" ? "1" : "any";
        input.min = setting.minimum;
        if (setting.maximum !== null) input.max = setting.maximum;
      }

    }
    input.value = value === null || value === undefined ? "" : value;
    input.setAttribute("aria-label", setting.label);
    if (setting.kind === "model") {
      input.dataset.modelPicker = "true";
      input.addEventListener("focus", () => refreshModels());
    }
    return input;
  }

  async function refreshModels() {
    if (!state.modelLookup) state.modelLookup = look("model/list");
    try {
      const reply = await state.modelLookup;
      const found = reply.servers.flatMap((server) => server.models || []);
      if (found.length) state.models = [...new Set(found)].sort();
      for (const select of document.querySelectorAll("[data-model-picker]")) {
        const current = select.value;
        const unset = select.querySelector('option[value=""]')?.textContent;
        select.replaceChildren();
        if (unset) {
          const option = el("option", null, unset);
          option.value = "";
          select.append(option);
        }
        for (const model of new Set([current, ...state.models].filter(Boolean))) {
          const option = el("option", null, model);
          option.value = model;
          select.append(option);
        }
        select.value = current;
      }
    } catch (_) {
      state.modelLookup = null;
    }
  }

  function workspaceControl(value, chosen) {
    const wrapper = el("div", "workspace-control");
    wrapper.append(el("span", "field-label", "Folder"));
    const line = el("div", "workspace-line");
    const path = el("span", "workspace-path", value || "Home");
    path.title = value || "Home";
    const button = el("button", "ghost", "Choose…");
    button.type = "button";
    button.setAttribute("aria-label", "Choose workspace");
    button.addEventListener("click", () => chooseWorkspace(value, async (folder) => {
      await chosen(folder, button);
      value = folder;
      path.textContent = folder;
      path.title = folder;
    }));
    line.append(path, button);
    wrapper.append(line);
    return wrapper;
  }

  function chooseWorkspace(initial, chosen) {
    const dialog = actionDialog("Choose workspace");
    dialog.classList.add("folder-dialog");
    const close = dialog.querySelector("button");
    close.textContent = "Cancel";
    const navigation = el("nav", "folder-navigation");
    navigation.setAttribute("aria-label", "Folder navigation");
    const home = el("button", "ghost", "Home");
    const up = el("button", "ghost", "↑ Parent folder");
    navigation.append(home, up);
    const current = el("nav", "folder-current");
    current.setAttribute("aria-label", "Current folder");
    const folders = el("div", "folder-list");
    const status = el("p", "error");
    status.setAttribute("role", "alert");
    const choose = el("button", "primary", "Use this folder");
    let selected = null;
    let version = 0;
    const show = async (path, after) => {
      const request = ++version;
      status.textContent = "";
      choose.disabled = true;
      if (!after) folders.replaceChildren(el("p", "sub", "Opening…"));
      try {
        const reply = await look("workspace/list", {
          ...(path ? { path } : {}), ...(after ? { after } : {}),
        });
        if (request !== version || !dialog.open) return;
        selected = reply.path;
        current.replaceChildren();
        const root = el("button", "ghost", "/");
        root.setAttribute("aria-label", "Filesystem root");
        root.addEventListener("click", () => show("/"));
        current.append(root);
        const parts = selected.split("/").filter(Boolean);
        parts.forEach((part, index) => {
          const path = "/" + parts.slice(0, index + 1).join("/");
          const crumb = el("button", "ghost", part);
          crumb.title = path;
          crumb.disabled = index === parts.length - 1;
          crumb.addEventListener("click", () => show(path));
          if (index) current.append(el("span", "sub", "/"));
          current.append(crumb);
        });
        up.disabled = !reply.parent;
        up.onclick = () => show(reply.parent);
        if (!after) folders.replaceChildren();
        for (const folder of reply.folders) {
          const button = el("button", "folder-choice", folder.name);
          button.type = "button";
          button.addEventListener("click", () => show(folder.path));
          folders.append(button);
        }
        if (reply.next_after) {
          const more = el("button", "ghost", "More folders");
          more.addEventListener("click", () => {
            more.remove();
            show(selected, reply.next_after);
          });
          folders.append(more);
        }
        if (!folders.children.length) folders.append(el("p", "sub", "No subfolders"));
        choose.disabled = false;
      } catch (error) {
        if (request === version) {
          selected = null;
          folders.replaceChildren();
          status.textContent = error.message;
        }
      }
    };
    home.addEventListener("click", () => show(null));
    const recent = el("details", "recent-folders");
    recent.append(el("summary", null, "Recent workspaces"));
    for (const path of [...new Set(state.sessions.map((row) => row.workspace))]
      .filter(Boolean).slice(0, 5)) {
      const button = el("button", "ghost", folderName(path));
      button.title = path;
      button.addEventListener("click", () => show(path));
      recent.append(button);
    }
    choose.addEventListener("click", async () => {
      if (!selected) return;
      choose.disabled = true;
      try {
        await chosen(selected);
        dialog.close();
      } catch (error) {
        status.textContent = error.message;
        choose.disabled = false;
      }
    });
    const footer = el("div", "folder-footer");
    footer.append(close, choose);
    dialog.append(navigation, current, folders, recent, status, footer);
    show(initial || null);
  }

  function runsWithSection(row) {
    const section = el("section");
    section.append(el("h2", null, "Session"));
    const form = el("div", "session-settings");
    const runs = state.runsWith || {};
    const inputs = {};
    const status = el("div", "sub save-status");
    status.setAttribute("aria-live", "polite");
    const apply = async (name, value, control) => {
      const previous = name === "thinking_effort" ? runs[name] : row[name];
      control.disabled = true;
      state.settingsDirty = true;
      status.textContent = "Updating…";
      try {
        const reply = await change("session/set", {
          session: row.id, [name]: value,
        });
        if (state.open === row.id) {
          state.runsWith = { ...state.runsWith, ...reply.set };
          Object.assign(rowOf(row.id) || row, reply.set);
        }
        Object.assign(runs, reply.set);
        status.textContent = reply.next_turn ? "Applies next turn" : "Updated";
      } catch (error) {
        if (control.tagName === "SELECT") control.value = previous || "";
        status.textContent = error.message;
        throw error;
      } finally {
        state.settingsDirty = false;
        control.disabled = false;
      }
    };
    for (const [name, label, value] of [
      ["mode", "Mode", row.mode], ["model", "Model", row.model],
      ["thinking_effort", "Thinking", runs.thinking_effort],
    ]) {
      const setting = state.settings.find((each) => each.name === name)
        || { kind: name === "thinking_effort" ? "choice" : name,
          choices: value ? [value] : [], label };
      const input = fieldInput({ ...setting, label }, value);
      const wrapper = el("label", null, label);
      wrapper.append(input);
      form.append(wrapper);
      inputs[name] = input;
      input.addEventListener("change", () => {
        apply(name, input.value, input).catch(() => {});
      });
    }
    form.append(workspaceControl(row.workspace, async (folder, button) => {
      await apply("workspace", folder, button);
    }), status);
    const capabilities = async () => {
      const model = inputs.model.value.trim();
      if (!model) return;
      const setting = state.settings.find((each) => each.name === "thinking_effort")
        || { needs: [] };
      try {
        if (!state.features.has(model)) {
          state.features.set(model, change("model/features", {
            model, cached_only: true,
          }));
        }
        const reply = await state.features.get(model);
        if (model !== inputs.model.value.trim()) return;
        for (const option of inputs.thinking_effort.options || []) {
          const need = (setting.needs || []).find(([value]) => value === option.value);
          option.disabled = Boolean(need && reply.features[need[1]] === false);
          option.title = option.disabled ? "Not supported by this model" : "";
        }
      } catch (error) { status.textContent = error.message; }
    };
    inputs.model.addEventListener("change", capabilities);
    capabilities();
    section.append(form);
    if (row.pending && row.pending.length) {
      section.append(el("p", "sub", `Next turn: ${row.pending.join(", ")}`));
    }
    return section;
  }

  function familySection(row) {
    let top = row;
    const ancestors = new Set([row.id]);
    while (top.parent && rowOf(top.parent) && !ancestors.has(top.parent)) {
      top = rowOf(top.parent);
      ancestors.add(top.id);
    }
    const children = new Map();
    for (const session of state.sessions) {
      if (!children.has(session.parent)) children.set(session.parent, []);
      children.get(session.parent).push(session);
    }
    if (top === row && !children.has(row.id)) return null;
    const section = el("section", "session-family");
    section.append(el("h2", null, "Session tree"));
    const tree = el("ul", "tree");
    tree.setAttribute("aria-label", "Related sessions");
    tree.append(sessionBranch(top, children, new Set(), true));
    section.append(tree);
    return section;
  }

  async function stopJob(job, button) {
    state.stoppingJobs.add(job.id);
    button.disabled = true;
    button.textContent = "Stopping…";
    try {
      await change("job/stop", { job: job.id });
    } catch (error) {
      state.stoppingJobs.delete(job.id);
      alert(error.message);
    } finally {
      button.blur();
      drawDetails();
    }
  }

  async function workSection(row) {
    const section = el("section");
    section.id = "background-jobs";
    section.append(el("h2", null, "Background jobs"));
    try {
      const reply = await look("job/list", { session: row.id });
      const running = reply.jobs.filter((job) => job.state === "running");
      if (running.length > 1) {
        const all = el("button", null, "Stop all");
        all.type = "button";
        all.addEventListener("click", async () => {
          all.disabled = true;
          all.textContent = "Stopping…";
          await Promise.all(running.map((job) => stopJob(job, all)));
        });
        section.append(all);
      }
      const groups = new Map();
      for (const [name, items] of [
        ["Running", running],
        ["Finished", reply.jobs.filter((job) => job.state !== "running")],
      ]) {
        const group = el("details", "job-group");
        group.dataset.group = name;
        const key = `${row.id}:${name}`;
        group.open = state.expandedJobs.get(key) ?? name === "Running";
        group.addEventListener("toggle", () => {
          if (group.isConnected) state.expandedJobs.set(key, group.open);
        });
        group.append(el("summary", null, `${name} · ${items.length}`));
        if (!items.length) group.append(el("p", "sub", "None"));
        groups.set(name, group);
        section.append(group);
      }
      for (const job of reply.jobs) {
        state.jobs.set(job.id, job);
        if (job.state !== "running") state.stoppingJobs.delete(job.id);
        for (const old of $("timeline").querySelectorAll("[data-job]")) {
          if (old.dataset.job === job.id) {
            const fresh = jobView(job);
            fresh.dataset.job = job.id;
            fresh.open = old.open;
            old.replaceWith(fresh);
          }
        }
        const item = jobView(job);
        const key = `${row.id}:job:${job.id}`;
        item.open = state.expandedJobs.get(key) || false;
        item.addEventListener("toggle", () => {
          if (item.isConnected) state.expandedJobs.set(key, item.open);
        });
        groups.get(job.state === "running" ? "Running" : "Finished").append(item);
      }

    } catch (error) {
      section.append(el("div", "error", error.message));
    }
    return section;
  }

  function jobView(job) {
    const item = el("details", "job");
    item.dataset.job = job.id;
    const status = job.stopped_by === "user" ? "Stopped by you"
      : job.state_text;
    item.append(el("summary", null, `${job.command} · ${status}`));
    item.append(el("div", "sub", job.id));
    if (job.exit_code !== null) {
      item.append(el("div", null, `Exit: ${job.exit_code}`));
    }
    if (job.state === "running") {
      const stop = el("button", null,
        state.stoppingJobs.has(job.id) ? "Stopping…" : "Stop");
      stop.type = "button";
      stop.disabled = state.stoppingJobs.has(job.id);
      stop.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        stopJob(job, stop);
      });
      item.querySelector("summary").append(stop);
    }
    const output = el("pre");
    item.append(output);
    item.addEventListener("toggle", async () => {
      if (!item.open) return;
      try {
        const reply = await look("job/read", { job: job.id });
        output.textContent = (reply.omitted_bytes
          ? `${reply.omitted_bytes} earlier bytes no longer retained.\n` : "")
          + (reply.output || "No output");
      } catch (error) { output.textContent = error.message; }
    });
    return item;
  }

  function sessionActions(row) {
    const menu = el("details", "session-actions");
    const summary = el("summary", null, "⋯");
    summary.setAttribute("aria-label", `Actions for ${row.title || row.id}`);
    menu.append(summary);
    const choices = el("div", "session-menu");
    choices.setAttribute("popover", "auto");
    menu.append(choices);
    menu.addEventListener("toggle", () => {
      if (!menu.open) { choices.hidePopover(); return; }
      const bounds = summary.getBoundingClientRect();
      choices.showPopover();
      choices.style.left = `${Math.max(8, Math.min(bounds.right - 160,
        window.innerWidth - 176))}px`;
      choices.style.top = `${Math.max(8, Math.min(bounds.bottom + 4,
        window.innerHeight - choices.offsetHeight - 8))}px`;
    });
    choices.addEventListener("toggle", () => {
      if (!choices.matches(":popover-open")) menu.open = false;
    });
    const actions = [
      ["Rename", async () => {
        const input = el("input", "rename");
        input.value = row.title || "";
        input.setAttribute("aria-label", "Session name");
        choices.replaceChildren(input);
        input.focus();
        input.select();
        input.addEventListener("keydown", async (event) => {
          if (event.key === "Escape") {
            menu.open = false;
            input.blur();
            drawSessions();
          }
          if (event.key === "Enter" && input.value.trim()) {
            input.disabled = true;
            try {
              const title = input.value.trim();
              await change("session/rename", { session: row.id, title });
              const current = rowOf(row.id);
              if (current) current.title = title;
              menu.open = false;
              input.blur();
              drawSessions();
            } catch (error) {
              input.disabled = false;
              input.focus();
              alert(error.message);
            }
          }
        });
      }],
      ["History…", () => historyActions(row.id)],
      ["Fork", async () => {
        const reply = await change("session/fork", {
          session: row.id, source: "web",
        });
        await openSession(reply.session.id);
      }],
      [row.archived ? "Restore" : "Archive", () => change(
        row.archived ? "session/unarchive" : "session/archive",
        { session: row.id })],
      ["Copy link", () => navigator.clipboard.writeText(
        `${location.origin}/#session=${encodeURIComponent(row.id)}`)],
      ["Delete", async () => {
        if (confirm("Move this session to the trash?")) {
          await change("session/delete", { session: row.id });
        }
      }],
    ];
    for (const [name, action] of actions) {
      const button = el("button", null, name);
      button.type = "button";
      button.addEventListener("click", () => {
        if (name !== "Rename") {
          menu.open = false;
          button.blur();
        }
        Promise.resolve(action()).catch((error) => alert(error.message));
      });
      choices.append(button);
    }
    return menu;
  }

  function drawUsage() {
    const context = state.context || {};
    const cache = context.cached_share;
    $("usage").textContent = `Context ${tokens(context.tokens)}`
      + (context.line ? ` / ${tokens(context.line)}` : "")
      + ` · Cache ${cache === null || cache === undefined
        ? "—" : `${Math.round(cache * 100)}%`}`;
    $("usage").title = "Cached input tokens in the last model call";
  }

  // ------------------------------------------------------------ server state

  function drawServer() {
    const footer = $("server-state");
    footer.replaceChildren();
    const version = state.hello ? state.hello.version : "";
    footer.append(el("span", null, version));
    if (state.asleep) {
      footer.append(el("span", "spacer"), el("span", null, "Asleep"));
      const button = el("button", "ghost", "Wake");
      button.type = "button";
      button.addEventListener("click", async () => {
        await look("server/hello").catch(() => null);
        connect();
      });
      footer.append(button);
      return;
    }
    footer.append(el("span", "spacer"), el("span", null, "On"));
  }

  // ------------------------------------------------------------ new session

  function openNew() {
    const dialog = $("new-dialog");
    const select = $("new-mode");
    select.replaceChildren();
    for (const each of (state.hello && state.hello.modes) || []) {
      const option = el("option", null, each.name);
      option.value = each.name;
      if (each.name === (state.hello.mode || "default")) option.selected = true;
      select.append(option);
    }
    select.title = modeAbout(select.value);
    const row = rowOf(state.open);
    if (!$("new-folder").value) {
      $("new-folder").value = state.launch.workspace || (row && row.workspace) || "/";
    }
    if (state.launch.mode) select.value = state.launch.mode;
    $("new-error").hidden = true;
    dialog.showModal();
  }

  $("new-folder-choose").addEventListener("click", () => {
    chooseWorkspace($("new-folder").value, async (folder) => {
      $("new-folder").value = folder;
    });
  });

  $("new-mode").addEventListener("change", () => {
    $("new-mode").title = modeAbout($("new-mode").value);
  });
  $("new-session").addEventListener("click", openNew);
  $("new-cancel").addEventListener("click", () => $("new-dialog").close());
  $("new-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = $("new-error");
    try {
      const reply = await change("session/send", {
        text: $("new-text").value.trim(), workspace: $("new-folder").value.trim(),
        mode: $("new-mode").value, source: "web",
        ...(state.launch.model ? { model: state.launch.model } : {}),
        ...(state.launch.thinking_effort
          ? { thinking_effort: state.launch.thinking_effort } : {}),
      });
      $("new-dialog").close();
      $("new-text").value = "";
      openSession(reply.session);
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    }
  });

  // ------------------------------------------------------------ layout

  function showPanel(name) {
    const frame = $("frame");
    frame.classList.remove("show-sessions", "show-session", "show-details");
    frame.classList.add(`show-${name}`);
    if (name === "details") frame.classList.remove("no-details");
  }

  $("back").addEventListener("click", () => showPanel("sessions"));
  $("details-toggle").addEventListener("click", () => {
    const frame = $("frame");
    if (window.matchMedia("(max-width: 760px)").matches) {
      showPanel(frame.classList.contains("show-details") ? "session" : "details");
      return;
    }
    frame.classList.toggle("no-details");
  });

  function stored(name) {
    try {
      return Number(localStorage.getItem(`zero.width.${name}`)) || null;
    } catch (error) {
      return null;
    }
  }

  function setWidth(name, value) {
    const [, low, high] = WIDTHS[name];
    const width = Math.max(low, Math.min(high, value));
    document.documentElement.style.setProperty(`--${name}`, `${width}px`);
    try {
      localStorage.setItem(`zero.width.${name}`, String(width));
    } catch (error) {
      // A private window keeps no widths
    }
  }

  for (const handle of document.querySelectorAll(".handle")) {
    const name = handle.dataset.handle;
    const width = stored(name);
    if (width) setWidth(name, width);
    handle.addEventListener("dblclick", () => setWidth(name, WIDTHS[name][0]));
    handle.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      const move = (moved) => setWidth(name, name === "left" ? moved.clientX
        : window.innerWidth - moved.clientX);
      const up = () => {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", up);
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", up);
    });
  }

  function browserCommands() {
    const descriptions = {
      "/fork": "fork this session into a new one",
      "/settings": "new-session defaults, model servers and advanced settings",
      "/help": "browser commands; Enter sends, Shift+Enter adds a line",
    };
    return Object.entries(state.hello?.commands || {}).filter(
      ([name]) => name !== "/quit").map(([name, about]) =>
      [name, descriptions[name] || about]);
  }

  function drawCommands(reset = true) {
    const list = $("commands");
    const text = $("text").value;
    list.replaceChildren();
    if (reset) state.commandIndex = 0;
    const matches = browserCommands().filter(
      ([name]) => text.startsWith("/") && !text.includes(" ")
        && name.startsWith(text));
    list.hidden = !matches.length;
    for (const [index, [name, about]] of matches.entries()) {
      const button = el("button", index === state.commandIndex ? "primary" : "",
        `${name} · ${about}`);
      button.type = "button";
      button.addEventListener("click", () => {
        $("text").value = `${name} `;
        saveComposerDraft();
        list.hidden = true;
        $("text").focus();
      });
      list.append(button);
    }
  }

  async function runCommand(text) {
    const [name] = text.split(/\s+/);
    if (name === "/quit") {
      alert("Close the browser tab to leave. ZERO keeps working.");
      return true;
    }
    if (!browserCommands().some(([command]) => command === name)) return false;
    const entered = $("text").value;
    const value = text.slice(name.length).trim();
    const session = state.open;
    try {
      switch (name) {
        case "/new": openNew(); break;
        case "/sessions": showPanel("sessions"); break;
        case "/settings": await openSettings(); break;
        case "/help":
          alert(browserCommands().map(
            ([command, about]) => `${command}: ${about}`).join("\n"));
          break;
        case "/jobs": showPanel("details"); break;
        case "/mode": case "/model": case "/cd":
          if (!value) { showPanel("details"); break; }
          await change("session/set", { session,
            [name === "/cd" ? "workspace" : name.slice(1)]: value });
          break;
        case "/rename":
          if (!value) throw new Error("Use /rename followed by a name");
          await change("session/rename", { session, title: value }); break;
        case "/archive":
          await change(rowOf(session).archived
            ? "session/unarchive" : "session/archive", { session }); break;
        case "/delete":
          if (confirm("Move this session to the trash?")) {
            await change("session/delete", { session });
          }
          break;
        case "/fork": {
          const reply = await change("session/fork", { session, source: "web" });
          await openSession(reply.session.id); break;
        }
        case "/compact":
          await change("session/compact", { session, instructions: value }); break;
        case "/continue":
          await change("session/continue", { session, source: "web" }); break;
        case "/copy": {
          const last = state.records.findLast((record) =>
            record.type === "assistant" && record.message.content);
          if (last) await navigator.clipboard.writeText(last.message.content);
          break;
        }
        case "/rewind": await historyActions(session); return true;
        case "/permissions":
          if (value.startsWith("forget ")) {
            const reply = await look("grant/list", { session });
            const index = Number(value.slice(7));
            const grant = Number.isInteger(index) && reply.grants[index - 1];
            if (!grant) throw new Error("Choose a listed permission number");
            await change("grant/forget", { session, grant: grant.id });
          } else await permissionsView(session);
          break;
        case "/init": {
          const reply = await change("session/send", {
            ...(session ? { session } : {}),
            source: "web", text: state.hello.init_request,
          });
          if (!session) await openSession(reply.session);
          break;
        }
        default: throw new Error("Unknown command");
      }
      if (readComposerDraft(session) === entered) writeComposerDraft(session, "");
      if (state.open === session && $("text").value === entered) {
        $("text").value = "";
        saveComposerDraft();
        sizeComposer();
        $("commands").hidden = true;
      }
    } catch (error) {
      alert(error.message);
    }
    return true;
  }

  function actionDialog(title) {
    const dialog = el("dialog", "dialog");
    dialog.append(el("h2", null, title));
    const close = el("button", null, "Back");
    close.addEventListener("click", () => dialog.close());
    dialog.append(close);
    dialog.addEventListener("close", () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
    return dialog;
  }

  async function historyActions(session) {
    const reply = await look("session/user_messages", { session });
    const dialog = actionDialog("Conversation history");
    dialog.append(el("p", "sub", "Rewind leaves files and memory unchanged."));
    for (const message of reply.messages) {
      const section = el("section");
      section.append(el("p", null, message.text));
      const button = el("button", null, "Rewind and edit");
      button.addEventListener("click", async () => {
        if (await rewindMessage(session, message.seq)) dialog.close();
      });
      section.append(button);
      dialog.append(section);
    }
  }

  async function permissionsView(session) {
    const reply = await look("grant/list", { session });
    const dialog = actionDialog("Permissions");
    for (const grant of reply.grants) {
      const section = el("section");
      section.append(el("p", null, grant.description));
      const forget = el("button", null, "Forget");
      forget.addEventListener("click", async () => {
        try {
          await change("grant/forget", { session, grant: grant.id });
          section.remove();
        } catch (error) { alert(error.message); }
      });
      section.append(forget);
      dialog.append(section);
    }
    if (!reply.grants.length) dialog.append(el("p", null, "No saved grants"));
  }

  async function openSettings(view = "General") {
    const dialog = $("settings-dialog");
    if (!dialog.open) {
      state.settingsDraft.clear();
      state.settingsErrors.clear();
      state.settings = (await look("setting/list")).settings;
      dialog.showModal();
    }
    state.settingsView = view;
    const tabs = $("settings-tabs");
    tabs.replaceChildren();
    for (const name of ["General", "Models", "Advanced"]) {
      const button = el("button", name === view ? "primary" : "", name);
      button.type = "button";
      button.addEventListener("click", () => {
        openSettings(name).catch((error) => {
          if (state.settingsView === name && dialog.open) {
            $("settings-body").textContent = error.message;
          }
        });
      });
      tabs.append(button);
    }
    const body = $("settings-body");
    body.replaceChildren(el("p", "sub settings-intro", view === "General"
      ? "Defaults for new sessions. Changes apply when you return to the conversation."
      : "Changes apply when you return to the conversation."));
    if (view === "General") {
      body.replaceChildren(el("h3", "settings-section-title", "Appearance"));
      const label = el("label", "setting-row", "Theme");
      const select = el("select");
      select.setAttribute("aria-label", "Theme");
      for (const name of ["System", "Light", "Dark"]) {
        const option = el("option", null, name);
        option.value = name.toLowerCase();
        select.append(option);
      }
      select.value = themePreference;
      select.addEventListener("change", () => {
        themePreference = select.value;
        try { localStorage.setItem("zero.theme", themePreference); }
        // This tab still uses the chosen theme if storage is unavailable.
        catch (error) {}
        applyTheme();
      });
      label.append(select);
      body.append(label, el("h3", "settings-section-title", "Defaults"),
        el("p", "sub", "For new sessions. Applied when you leave settings."));
    }
    if (view === "Models") return modelServers(body);
    for (const setting of state.settings.filter((each) => each.shown
      && (view === "General" ? each.group === "Default"
        : each.group !== "Default"))) {
      const form = el("div", "setting-row");
      const label = el("label", null, setting.label);
      const value = state.settingsDraft.has(setting.name)
        ? state.settingsDraft.get(setting.name) : setting.value;
      const input = fieldInput(setting, value);
      input.dataset.setting = setting.name;
      label.append(input);
      const status = el("span", "error setting-error",
        state.settingsErrors.get(setting.name) || "");
      status.hidden = !status.textContent;
      form.append(label, status);
      input.addEventListener("input", () => {
        state.settingsDraft.set(setting.name, input.value);
        state.settingsErrors.delete(setting.name);
        status.hidden = true;
      });
      body.append(form);
    }
  }

  async function leaveSettings() {
    if (state.settingsSaving) return;
    state.settingsSaving = true;
    const back = $("settings-close");
    back.disabled = true;
    back.textContent = "Updating…";
    const controls = [...$("settings-dialog").querySelectorAll(
      "input, select, button")].filter((control) => !control.disabled);
    for (const control of controls) control.disabled = true;
    try {
      for (const [name, raw] of state.settingsDraft) {
        const setting = state.settings.find((each) => each.name === name);
        try {
          let value = raw;
          if (raw === "" && setting.unset_means) value = null;
          else if (["int", "number"].includes(setting.kind)) {
            if (raw === "" || !Number.isFinite(Number(raw))) {
              throw new Error("Enter a number");
            }
            value = Number(raw);
          }
          await change("setting/set", { name, value });
          setting.value = value;
          state.settingsDraft.delete(name);
          state.settingsErrors.delete(name);
        } catch (error) {
          state.settingsErrors.set(name, error.message);
          await openSettings(setting.group === "Default" ? "General" : "Advanced");
          $("settings-body").querySelector(`[data-setting="${name}"]`)?.focus();
          return;
        }
      }
      $("settings-dialog").close();
      drawDetails();
    } finally {
      state.settingsSaving = false;
      for (const control of controls) control.disabled = false;
      back.disabled = false;
      back.textContent = "Back";
    }
  }

  async function modelServers(body) {
    const reply = await look("model/server/list");
    if (state.settingsView !== "Models" || !$("settings-dialog").open) return;
    body.replaceChildren(el("p", "sub",
      "Connecting tests a few tokens with the model server, which may charge."));
    for (const server of [...reply.servers,
      { name: "", title: "Add a model server", url: "", key_state: "" }]) {
      const section = el("section");
      section.append(el("h3", null, server.title || server.name),
        el("p", "sub", `${server.url} · ${server.key_state}`));
      const form = el("form", "model-server");
      const name = el("input");
      name.placeholder = "Server name";
      name.setAttribute("aria-label", "Server name");
      const url = el("input");
      url.type = "url";
      url.placeholder = "API URL";
      url.setAttribute("aria-label", "API URL");
      if (!server.name) form.append(name, url);
      const model = el("input");
      model.placeholder = "Model name";
      model.setAttribute("aria-label", "Model name");
      const key = el("input");
      key.type = "password";
      key.autocomplete = "off";
      key.placeholder = "New key (blank keeps the existing key)";
      key.setAttribute("aria-label", "Model server key");
      const connect = el("button", null, "Test and connect");
      connect.type = "submit";
      const status = el("div", "sub");
      form.append(model, key, connect, status);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        connect.disabled = true;
        try {
          const fields = { name: server.name || name.value.trim(),
            model: model.value.trim() };
          if (!server.name) fields.url = url.value.trim();
          if (key.value) fields.key = key.value;
          await change("model/server/connect", fields);
          key.value = "";
          state.modelLookup = null;
          refreshModels();
          status.textContent = "Connected";
        } catch (error) { status.textContent = error.message; }
        finally { connect.disabled = false; }
      });
      section.append(form);
      if (server.name) {
        const disconnect = el("button", null, "Disconnect");
        disconnect.addEventListener("click", async () => {
          if (!confirm(`Remove the saved connection to ${server.name}?`)) return;
          try {
            await change("model/server/disconnect", { name: server.name });
            await openSettings("Models");
          } catch (error) { status.textContent = error.message; }
        });
        section.append(disconnect);
      }
      body.append(section);
    }
  }

  $("settings-open").addEventListener("click", () => {
    openSettings().catch((error) => alert(error.message));
  });
  $("settings-close").addEventListener("click", leaveSettings);
  $("settings-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    leaveSettings();
  });

  // ------------------------------------------------------------ start

  async function pairBrowser() {
    const fragment = new URLSearchParams(location.hash.slice(1));
    state.launch = Object.fromEntries(fragment);
    delete state.launch.pair;
    const paired = fragment.get("pair");
    if (paired) {
      localStorage.setItem("zero.browser", paired);
      fragment.delete("pair");
      history.replaceState(null, "", location.pathname
        + (fragment.size ? `#${fragment}` : ""));
    }
    const key = localStorage.getItem("zero.browser");
    if (!key) return;
    const response = await fetch("/api/browser", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    });
    if (!response.ok) {
      throw new Error("This browser is not paired. Run zero web once.");
    }
  }

  async function start() {
    showPanel("sessions");
    drawDetails();
    drawAsk();
    try {
      await pairBrowser();
      state.hello = await look("server/hello");
      const settings = await look("setting/list").catch(() => ({ settings: [] }));
      state.settings = settings.settings;
      const listed = await look("model/server/list").catch(() => ({ servers: [] }));
      state.models = listed.servers.flatMap((server) =>
        Object.keys(server.models || {}).map((model) => `${server.name}/${model}`));
    } catch (error) {
      $("server-state").textContent = error.message;
      return;
    }
    drawServer();
    const selected = new URLSearchParams(location.hash.slice(1)).get("session");
    const listed = await look("session/list", { archived: null });
    state.sessions = listed.sessions;
    drawSessions();
    let target = selected;
    if (!target && state.launch.view !== "new"
        && state.launch.view !== "sessions") {
      const workspace = state.launch.workspace;
      const rows = listed.sessions.filter((row) => !row.archived
        && (!workspace || workspace === "/" || row.workspace === workspace));
      rows.sort((a, b) => b.updated_at - a.updated_at);
      target = rows[0] && rows[0].id;
    }
    if (target) {
      const matches = listed.sessions.filter((row) => row.id.startsWith(target));
      if (matches.length === 1) target = matches[0].id;
      const fields = Object.fromEntries(["model", "mode", "thinking_effort"]
        .filter((name) => state.launch[name])
        .map((name) => [name, state.launch[name]]));
      if (Object.keys(fields).length) {
        await change("session/set", { session: target, ...fields });
      }
      await openSession(target);
    } else connect();
    if (!state.open && !$("new-dialog").open
        && (state.launch.view === "new" || (!target && state.launch.workspace
        && state.launch.view !== "sessions"))) openNew();
    sizeComposer();
  }

  $("archived").addEventListener("click", () => {
    state.archived = !state.archived;
    $("archived").textContent = state.archived ? "Active sessions" : "Archived";
    $("archived").setAttribute("aria-label", $("archived").textContent);
    drawSessions();
  });

  let themePreference = "system";
  try { themePreference = localStorage.getItem("zero.theme") || "system"; }
  // System appearance works without browser storage.
  catch (error) {}
  const darkScheme = matchMedia("(prefers-color-scheme: dark)");
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
  function applyTheme() {
    const dark = themePreference === "dark"
      || (themePreference === "system" && darkScheme.matches);
    const theme = dark ? "dark" : "light";
    document.documentElement.dataset.theme = theme;
    const still = reducedMotion.matches ? "-still" : "";
    document.querySelectorAll(".brand source").forEach((source) => {
      source.srcset = `/ui/wordmark-${theme}${still}.svg`;
    });
    document.querySelector(".brand img").src = `/ui/wordmark-${theme}${still}.svg`;
    document.querySelectorAll('link[rel="icon"]').forEach((icon) => {
      icon.removeAttribute("media");
      icon.href = `/ui/favicon-${theme}.svg`;
    });
  }
  darkScheme.addEventListener("change", applyTheme);
  reducedMotion.addEventListener("change", applyTheme);
  applyTheme();
  start();
})();
