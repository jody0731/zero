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
    hello: null, sessions: [], open: null, newSession: null, records: [], earlier: 0,
    runsWith: null, context: null, events: null, asleep: false,
    calls: new Map(), lastSeq: 0, lastTime: null, draft: null,
    hiddenTimer: null, seenSent: 0, turn: null, archived: false,
    stoppingJobs: new Set(), detailsVersion: 0,
    settings: [], commandIndex: 0, launch: {},
    expandedJobs: new Map(), expandedFamilies: new Map(),
    settingsDraft: new Map(), settingsErrors: new Map(), settingsSaving: false,
    settingsView: "General", modelLookup: null, modelServer: null,
    modelDirectories: new Map(), modelVersions: new Map(), modelPanelVersion: 0,
    rewindPoints: new Set(), composerDrafts: new Map(), submissions: new Map(),
    jobs: new Map(), settingsDirty: false, features: new Map(), turnStarted: null,
  };

  try {
    state.submissions = new Map(JSON.parse(sessionStorage.getItem("zero.submissions")
      || "[]"));
  } catch {
    // This tab can still track submissions in memory.
  }

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
    if (!reply.ok) {
      const error = new Error(reply.error);
      error.modelFailure = reply.model_failure;
      throw error;
    }
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

  // ------------------------------------------------------------ stream

  function connect() {
    if (state.events) state.events.close();
    const query = state.open
      ? `?session=${encodeURIComponent(state.open)}&after=${state.lastSeq}`
      : "";
    const events = new EventSource(`/api/events${query}`);
    state.events = events;
    events.addEventListener("list", (event) => {
      if (state.events !== events) return;
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
      if (state.events !== events) return;
      checkDeliveries(state.open);
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
    if (event.withdrawn) {
      restoreWithdrawn(state.open, event.withdrawn.map(item => item.id));
      return;
    }
    if (event.record) {
      if (state.submissions.delete(event.record.inbox)) saveSubmissions();
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
      if (state.turn) {
        if (event.settled.ask) showProgress(state.turn, state.turn.phase, true);
        else finishProgress(state.turn, "Completed");
      }
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

  async function openSession(id, draft = null) {
    const version = (state.openVersion || 0) + 1;
    state.openVersion = version;
    if (state.events) state.events.close();
    state.events = null;
    saveComposerDraft();
    $("title-editor")?.remove();
    $("session-title").hidden = false;
    state.open = id;
    state.newSession = draft;
    state.runsWith = draft;
    state.context = null;
    $("queue").checked = false;
    state.rewindPoints = new Set();
    restoreComposerDraft();
    history.replaceState(null, "", id ? `#session=${encodeURIComponent(id)}`
      : "#view=new");
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
    if (draft) {
      timeline.replaceChildren();
      drawDetails();
      drawAsk();
      connect();
      $("text").focus();
      return;
    }
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
    state.newSession = null;
    $("title-editor")?.remove();
    state.turn = null;
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
    const draft = state.draft;
    const progressOpen = state.turn?.process.open;
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
    if (draft) drawDraft(draft);
    if (state.turn && progressOpen !== undefined) state.turn.process.open = progressOpen;
    drawAsk();
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
    const row = state.newSession || rowOf(state.open);
    $("session-title").textContent = row ? row.title || row.id : state.open || "";
    $("session-title").disabled = !row;
  }

  $("session-title").addEventListener("click", () => {
    const draft = state.newSession;
    const id = state.open;
    const button = $("session-title");
    const input = el("input");
    input.id = "title-editor";
    input.setAttribute("aria-label", "Session name");
    input.value = button.textContent;
    button.hidden = true;
    button.after(input);
    input.focus();
    input.select();
    let finished = false;
    const finish = async (save, focus) => {
      if (finished || !input.isConnected) return;
      finished = true;
      const title = input.value.trim();
      input.remove();
      button.hidden = false;
      if (focus) button.focus();
      if (!save || !title || title === button.textContent) return;
      try {
        if (draft) draft.title = title;
        else {
          await change("session/rename", { session: id, title });
          const row = rowOf(id);
          if (row) row.title = title;
        }
        drawHead();
        drawSessions();
      } catch (error) { alert(error.message); }
    };
    input.addEventListener("blur", () => finish(true, false));
    input.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === "Escape") {
        event.preventDefault();
        finish(event.key === "Enter", true);
      }
    });
  });

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
      if (state.turn) finishProgress(state.turn, "Progress");
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
        turnFor(record);
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
      case "retry": return modelFailureEntry(record);
      case "note": return record.model_failure
        ? modelFailureEntry(record) : noteEntry(record);
      case "compaction":
        return el("div", "divider", `Compacted ${tokens(record.tokens_before)}`
          + ` → ${tokens(record.tokens_after)}`);
      case "compaction_failed":
        return record.model_failure ? modelFailureView(record.model_failure)
          : el("div", "divider", "Compaction failed");
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
      node.append(who(record.interaction?.sender_session ? record.sender_name : name,
        record));
      node.append(el("div", "bubble", record.text || ""));
      if (record.reference) node.append(jobLink(record.reference));
      return node;
    }
    const more = record.interaction?.action === "queue" ? "queued" :
      record.steer ? "steer" : (record.continue ? "go on" : "");
    node.append(who(record.sender_name || sourceWho(record.source), record, more));
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
    const liveStep = el("div", "process-step live-step", "Thinking");
    process.append(summary, liveStep);
    const progress = el("div", "turn-progress active");
    progress.append(process);
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
    node.append(replies, progress, copy);
    $("timeline").append(node);
    state.turn = { node, progress, process, summary, replies, copy, texts, liveStep,
      phase: "Thinking" };
    return state.turn;
  }

  function assistantEntry(record) {
    const message = record.message || {};
    const turn = turnFor(record);
    turn.liveStep.remove();
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
    const fields = record.reply;
    const text = message.content || [fields?.answer, fields?.why, fields?.next?.user,
      fields?.next?.zero].filter(value => value?.trim()).join("\n\n");
    if (text.trim()) {
      const reply = el("div", "public-reply");
      drawPublicReply(reply, fields, text, record.seq);
      turn.replies.append(reply);
      turn.texts.push(text);
      turn.copy.hidden = false;
    }
    showProgress(turn, message.tool_calls?.length ? "Working" : "Thinking");
    for (const call of message.tool_calls || []) {
      node.append(workEntry(call.id, call.function?.name, call.function?.arguments));
    }
    return null;
  }

  function workEntry(call, name, args) {
    const fold = el("details", "call");
    fold.dataset.call = call;
    fold.append(el("summary", null, "Working"), el("div", "body", "In progress"));
    if (name) fold.append(el("strong", null, name));
    if (args && args !== "{}") fold.append(el("pre", null, args));
    state.calls.set(call, fold);
    return fold;
  }

  function jobLink(id) {
    const link = el("button", null, "Show job");
    link.type = "button";
    link.addEventListener("click", () => {
      showPanel("details");
      const view = Array.from($("details").querySelectorAll("[data-job]"))
        .find(item => item.dataset.job === id);
      if (!view) return;
      view.closest(".job-group").open = true;
      view.open = true;
      view.scrollIntoView();
    });
    return link;
  }

  function toolEntry(record) {
    let fold = state.calls.get(record.call);
    if (!fold) {
      // The history window can start between a call and its result.
      fold = workEntry(record.call, record.tool, record.arguments);
      const step = el("div", "process-step");
      step.dataset.seq = record.seq;
      step.append(fold);
      const turn = turnFor(record);
      turn.liveStep.remove();
      showProgress(turn, "Working");
      turn.process.append(step);
    }
    fold.querySelector(".body").textContent = record.denied ? "Declined"
      : record.failed || record.bad_call ? "Failed" : "Finished";
    const result = record.result || "";
    if (result) {
      const output = result.length > RESULT_CHARS
        ? result.slice(0, RESULT_CHARS) + `\n… +${result.length - RESULT_CHARS}`
        : result;
      fold.append(el("pre", null, output));
    }
    if (record.saved_path) fold.append(el("div", "saved", record.saved_path));
    if (record.fields?.job_id) fold.append(jobLink(record.fields.job_id));
    return null;
  }

  function askEntry(record) {
    const turn = turnFor(record);
    showProgress(turn, turn.phase, true);
    return null;
  }

  function answerEntry(record) {
    const turn = turnFor(record);
    showProgress(turn, turn.phase, false);
    const node = el("div", "entry you");
    const event = record.interaction;
    const action = { approve: "approved", decline: "declined" }[event?.action]
      || "answered";
    const target = event?.target?.ask;
    node.append(who(record.sender_name || sourceWho(record.source), record,
      [action, target].filter(Boolean).join(" · ")));
    if (record.question_text) {
      node.append(el("div", "meta", record.question_text));
    }
    const words = record.text || record.choice || (record.yes ? "yes" : "no");
    node.append(el("div", "bubble", words));
    return node;
  }

  function turnEnd(record) {
    const failed = record.outcome === "failed" || record.error;
    const text = [record.outcome, seconds(record.seconds),
      record.error || record.detail || ""].filter(Boolean).join(" · ");
    const turn = turnFor(record);
    if (record.model_failure) {
      turn.replies.append(modelFailureView(record.model_failure));
    }
    if (!turn.texts.length && record.reply && record.answer) {
      const reply = el("div", "public-reply");
      drawPublicReply(reply, record.reply, record.answer, record.seq);
      turn.replies.append(reply);
      turn.texts.push(record.answer);
      turn.copy.hidden = false;
    }
    finishProgress(turn, record.model_failure
      ? `Stopped · ${seconds(record.seconds)}` : text || "Completed");
    if (failed) turn.summary.classList.add("failed");
    return null;
  }

  function modelFailureView(failure) {
    const node = el("div", "model-failure");
    const title = failure.server_title || failure.server;
    node.append(el("strong", null, `Model request failed · ${title}`));
    if (failure.next_action === "retry") {
      node.append(el("div", "sub", "Retrying model request…"));
    } else if (failure.next_action === "fallback") {
      node.append(el("div", "sub", `Using fallback · ${failure.fallback}`));
    }
    const operations = {
      generate_reply: "Generate reply", test_model: "Test model",
      compact_context: "Compact context",
    };
    const lines = [
      operations[failure.operation] || failure.operation,
      failure.http_status == null ? null : `HTTP ${failure.http_status}`,
      failure.provider_code, failure.provider_message, failure.observation,
    ];
    node.append(failureDetails(lines.filter(Boolean)));
    return node;
  }

  function failureDetails(lines) {
    const details = el("details", "model-failure-details");
    details.append(el("summary", null, "Details"));
    for (const line of lines) details.append(el("div", null, line));
    return details;
  }

  function modelFailureEntry(record) {
    if (record.model_failure) {
      const turn = turnFor(record);
      turn.replies.append(modelFailureView(record.model_failure));
    }
    return null;
  }

  function showProgress(turn, phase,
                        waiting = turn.progress.classList.contains("waiting")) {
    turn.phase = phase;
    turn.progress.classList.toggle("waiting", waiting);
    const label = waiting ? "Waiting for you" : phase + "…";
    if (turn.summary.textContent !== label) turn.summary.textContent = label;
  }

  function finishProgress(turn, label) {
    turn.liveStep.remove();
    for (const fold of state.calls.values()) {
      const status = fold.querySelector(".body");
      if (status.textContent === "In progress") status.textContent = "No result recorded";
    }
    turn.summary.textContent = label;
    turn.progress.classList.remove("active", "waiting");
    turn.node.insertBefore(turn.progress, turn.replies);
    state.turn = null;
    state.calls.clear();
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
    state.draft = draft;
    const turn = turnFor({});
    const following = atEnd();
    let node = $("draft");
    if (!node) {
      node = el("div", "entry zero draft");
      node.id = "draft";
      node.append(el("div", "public-reply"));
    }
    const text = draft.reply && !draft.reply.answer?.trim() ? "" : draft.text;
    drawPublicReply(node.firstElementChild, draft.reply, text, state.lastSeq + 1);
    const phase = draft.calls?.length || text ? "Working" : "Thinking";
    showProgress(turn, phase);
    turn.liveStep.textContent = phase;
    if (!turn.liveStep.parentNode) turn.process.append(turn.liveStep);
    node.hidden = !text;
    if (node.parentNode !== turn.replies) turn.replies.append(node);
    if (following) scrollToEnd();
  }

  function drawPublicReply(node, reply, fallback, seq) {
    // A saved reply from an older release can have only Why or Next. Its
    // plain projection remains readable. In a draft, wait for Answer.
    if (!reply?.answer?.trim()) reply = null;
    const answer = reply ? reply.answer : fallback;
    const parts = [
      ["answer", [answer]], ["why", [reply?.why]],
      ["next", [reply?.next?.user, reply?.next?.zero]],
    ];
    for (const [name, fields] of parts) {
      const values = fields.filter(value => value?.trim());
      let section = node.querySelector(`[data-section="${name}"]`);
      if (!values.length) {
        section?.remove();
        continue;
      }
      if (!section) {
        section = el("div");
        section.dataset.section = name;
        const body = el("div");
        if (name !== "answer") {
          const label = name === "why" ? "Why" : "Next";
          const key = `zero.reply.${state.open}.${seq}.${name}`;
          body.id = key;
          const button = el("button", "reply-label", label);
          button.type = "button";
          button.setAttribute("aria-controls", key);
          try { body.hidden = sessionStorage.getItem(key) === "folded"; }
          catch { /* This browser keeps fold state only in the open reply. */ }
          button.setAttribute("aria-expanded", String(!body.hidden));
          button.addEventListener("click", () => {
            body.hidden = !body.hidden;
            button.setAttribute("aria-expanded", String(!body.hidden));
            try {
              if (body.hidden) sessionStorage.setItem(key, "folded");
              else sessionStorage.removeItem(key);
            } catch { /* Folding also works without browser storage. */ }
          });
          section.append(button);
        }
        section.append(body);
      }
      if (section.parentNode !== node) {
        const later = name === "answer" ? node.firstElementChild
          : name === "why" ? node.querySelector('[data-section="next"]') : null;
        node.insertBefore(section, later);
      }
      const body = section.lastElementChild;
      values.forEach((value, index) => {
        const previous = body.children[index];
        if (previous?.sourceText === value) return;
        const current = markdown(value);
        current.sourceText = value;
        if (previous) previous.replaceWith(current);
        else body.append(current);
      });
      while (body.children.length > values.length) body.lastElementChild.remove();
    }
  }

  function clearDraft() {
    state.draft = null;
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
    const waiting = Boolean(ask) || row?.state === "waiting";
    if (state.turn && (waiting || !box.hidden)) {
      showProgress(state.turn, state.turn.phase, waiting);
    }
    $("composer").hidden = !row && !state.newSession;
    $("queue").parentElement.hidden = Boolean(state.newSession) || waiting;
    $("stop").hidden = !row || row.state !== "running" || waiting;
    $("text").placeholder = waiting ? "Queue a separate message" : "Message";
    $("text").setAttribute("aria-label", $("text").placeholder);
    $("send").textContent = waiting ? "Queue message" : "Send";
    if (!ask) {
      box.hidden = true;
      box.replaceChildren();
      return;
    }
    if (box.dataset.ask === ask.id && !box.hidden) return;
    box.dataset.ask = ask.id;
    box.hidden = false;
    box.replaceChildren();
    if (ask.kind === "secret") {
      drawVaultInput(box, ask);
      return;
    }
    const head = el("h2", null,
      ask.kind === "question" ? "Question" : "Permission");
    if (ask.about) head.title = ask.about;
    box.append(head);
    box.append(el("pre", null, ask.command || ask.tool || ""));
    if (ask.reason) box.append(el("div", null, ask.reason));
    const choices = el("div", "choices");
    for (const choice of ask.choices || []) {
      const label = (ask.choice_labels || {})[choice] || choice;
      const button = el("button", choice === (ask.choices || [])[0]
        ? "primary" : "", ask.kind === "question"
          ? `${choice}. ${label}` : label);
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

  function drawVaultInput(box, ask) {
    box.append(el("h2", null, ask.about || "Private information"));
    if (ask.about && ask.why) {
      box.append(el("p", "private-instructions", ask.why));
    }
    box.append(el("p", null,
      "Saved encrypted on this device until you delete it. " +
      "The value stays outside the conversation."));
    const form = el("form", "words vault-fields");
    const fields = new Map();
    for (const part of ask.choices || []) {
      const words = (ask.choice_labels || {})[part] || part;
      const label = el("label", "private-instructions", words);
      const input = el("input");
      input.type = "password";
      input.autocomplete = "new-password";
      input.required = true;
      input.setAttribute("aria-label", words.split("\n")[0]);
      label.append(input);
      form.append(label);
      fields.set(part, input);
    }
    const save = el("button", "primary", "Continue");
    save.type = "submit";
    const cancel = el("button", null, "Cancel");
    cancel.type = "button";
    async function submit(cancelled) {
      const values = {};
      for (const [part, input] of fields) {
        values[part] = input.value;
        input.value = "";
      }
      save.disabled = cancel.disabled = true;
      try {
        await change("vault/answer", {
          session: state.open, ask: ask.id,
          ...(cancelled ? { cancel: true } : { values }),
        });
      } catch (error) {
        box.append(el("div", "error", error.message));
        save.disabled = cancel.disabled = false;
      } finally {
        for (const part of Object.keys(values)) delete values[part];
      }
    }
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      submit(false);
    });
    cancel.addEventListener("click", () => submit(true));
    form.append(save, cancel);
    box.append(form);
  }

  async function answer(ask, given) {
    const box = $("ask");
    for (const button of box.querySelectorAll("button")) button.disabled = true;
    try {
      await change("session/submit", {
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
    const original = $("text").value;
    const button = $("send");
    let text = original.trim();
    if (!text || button.disabled) return;
    const draft = state.newSession;
    const session = state.open;
    if (!session && !draft) return;
    button.disabled = true;
    try {
      if (draft && text === "/init") text = state.hello.init_request;
      else if (await runCommand(text)) return;
      if (draft) {
        const fields = { ...draft, text, source: "web" };
        const content = JSON.stringify(fields);
        if (state.firstSend?.content !== content) {
          state.firstSend = { content, id: newId() };
        }
        const reply = await change("session/send", {
          ...fields, id: state.firstSend.id,
        });
        if (state.newSession === draft) {
          const remaining = $("text").value;
          if (remaining !== original) writeComposerDraft(reply.session, remaining);
          await openSession(reply.session);
        }
        return;
      }
      const id = newId();
      const queued = Boolean(rowOf(session)?.ask) || $("queue").checked;
      const receipt = await change("session/submit", {
        id, session, text, source: "web", when_busy: queued ? "queue" : "steer",
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
      if (["steered", "queued"].includes(receipt.handled_as)) {
        state.submissions.set(id, { session, text: original });
        saveSubmissions();
        // A stop or delivery may have preceded the acknowledgement.
        checkDeliveries(session);
      }
    } catch (error) {
      alert(error.message);
    } finally {
      button.disabled = false;
    }
  }

  function saveSubmissions() {
    try {
      const pending = JSON.stringify([...state.submissions]);
      sessionStorage.setItem("zero.submissions", pending);
    } catch {
      // Keep tracking in memory when this tab cannot use session storage.
    }
  }

  function restoreWithdrawn(session, ids) {
    const texts = [];
    for (const id of ids) {
      const message = state.submissions.get(id);
      if (message?.session !== session) continue;
      texts.push(message.text);
      state.submissions.delete(id);
    }
    if (!texts.length) return;
    const draft = state.open === session ? $("text").value : readComposerDraft(session);
    writeComposerDraft(session, [...texts, draft].filter(Boolean).join("\n\n"));
    if (state.open === session) restoreComposerDraft();
    saveSubmissions();
  }

  async function checkDeliveries(session) {
    const ids = [...state.submissions].filter(([, m]) => m.session === session)
      .map(([id]) => id);
    const withdrawn = [];
    try {
      for (let i = 0; i < ids.length; i += 100) {
        const batch = ids.slice(i, i + 100);
        const reply = await look("session/deliveries", {
          session, ids: batch,
        });
        const statuses = reply.deliveries || {};
        for (const id of batch) {
          if (statuses[id] === "withdrawn") withdrawn.push(id);
          else if (["delivered", "done"].includes(statuses[id])) {
            state.submissions.delete(id);
          }
        }
      }
      restoreWithdrawn(session, withdrawn);
      if (ids.length) saveSubmissions();
    } catch {
      // Outstanding messages are checked again on reconnect.
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
      const session = state.open;
      const receipt = await change("session/stop", { session, source: "web" });
      restoreWithdrawn(session, (receipt.withdrawn || []).map(item => item.id));
    } catch (error) {
      alert(error.message);
    }
  });

  // ------------------------------------------------------------ details

  async function drawDetails() {
    const panel = $("details");
    const draft = state.newSession;
    const row = draft || rowOf(state.open);
    drawUsage();
    const version = ++state.detailsVersion;
    if (draft && panel.draftRow === draft) return;
    panel.draftRow = draft;
    const editing = state.settingsDirty || panel.contains(document.activeElement);
    if (!draft && row && editing) {
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
      if (state.open || !state.hello) return;
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
    if (draft) return;
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
          ? modelChoices(value)
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
      if (setting.kind === "string" && setting.unset_means) {
        input.placeholder = setting.unset_means;
      }
    }
    input.value = value === null || value === undefined ? "" : value;
    input.setAttribute("aria-label", setting.label);
    if (setting.kind === "model") {
      input.dataset.modelPicker = "true";
      input.addEventListener("focus", () => refreshModels());
      input.addEventListener("pointerdown", () => refreshModels());
    }
    return input;
  }

  function modelChoices(current) {
    const models = [...state.modelDirectories.values()].flatMap((row) => row.models);
    return [...new Set([current, ...models.sort()].filter(Boolean))];
  }

  function updateModelPickers() {
    for (const select of document.querySelectorAll("[data-model-picker]")) {
      const current = select.value;
      const unset = select.querySelector('option[value=""]');
      select.replaceChildren(...(unset ? [unset] : []));
      for (const model of modelChoices(current)) {
        const option = el("option", null, model);
        option.value = model;
        select.append(option);
      }
      select.value = current;
    }
  }

  function nextModelVersion(name) {
    const version = (state.modelVersions.get(name) || 0) + 1;
    state.modelVersions.set(name, version);
    return version;
  }

  function acceptModelDirectory(name, version, row) {
    if (state.modelVersions.get(name) !== version) return false;
    nextModelVersion(name);
    if (row) state.modelDirectories.set(name, row);
    else state.modelDirectories.delete(name);
    updateModelPickers();
    return true;
  }

  async function refreshModels() {
    if (state.modelLookup) return;
    const versions = new Map(state.modelVersions);
    const pending = look("model/list", { cached: true });
    state.modelLookup = pending;
    try {
      const reply = await pending;
      const listed = new Map(reply.servers.map((row) => [row.server, row]));
      const names = new Set([...state.modelDirectories.keys(), ...listed.keys()]);
      for (const name of names) {
        // A key change or explicit directory refresh may have answered first.
        if (versions.get(name) !== state.modelVersions.get(name)) continue;
        if (listed.has(name)) state.modelDirectories.set(name, listed.get(name));
        else state.modelDirectories.delete(name);
      }
      updateModelPickers();
    } catch (_) {
      // Keep the last directory if the local server couldn't be reached.
    } finally {
      if (state.modelLookup === pending) state.modelLookup = null;
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
      if (row === state.newSession) {
        row[name] = value;
        return;
      }
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
              drawHead();
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
    $("usage").hidden = Boolean(state.newSession);
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
    const defaults = Object.fromEntries(state.settings.map(s => [s.name, s.value]));
    const { workspace, mode, model, thinking_effort } = state.launch;
    state.firstSend = null;
    openSession(null, {
      title: "New session", workspace: workspace || "/",
      mode: mode || defaults.mode || state.hello?.mode || "default",
      model: model || defaults.model || state.hello?.model,
      thinking_effort: thinking_effort || defaults.thinking_effort || undefined,
    });
  }

  $("new-session").addEventListener("click", openNew);

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
      if (state.newSession && !["/new", "/sessions", "/settings", "/help",
        "/rename", "/mode", "/model", "/cd", "/init"].includes(name)) {
        throw new Error("Send the first message to use this session command.");
      }
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
          if (state.newSession) {
            state.newSession[name === "/cd" ? "workspace" : name.slice(1)] = value;
            $("details").draftRow = null;
            drawDetails();
            break;
          }
          await change("session/set", { session,
            [name === "/cd" ? "workspace" : name.slice(1)]: value });
          break;
        case "/rename":
          if (!value) throw new Error("Use /rename followed by a name");
          if (state.newSession) { state.newSession.title = value; drawHead(); }
          else await change("session/rename", { session, title: value });
          break;
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
          const reply = await change("session/submit", {
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
    const groups = new Map();
    for (const setting of state.settings.filter((each) => each.shown
      && (view === "General" ? each.group === "Default"
        : each.group !== "Default"))) {
      let container = body;
      if (view === "Advanced") {
        container = groups.get(setting.group);
        if (!container) {
          container = el("section", "settings-group");
          container.setAttribute("aria-label", setting.group);
          container.append(el("h3", "settings-section-title", setting.group));
          groups.set(setting.group, container);
          body.append(container);
        }
      }
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
      container.append(form);
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

  const modelKeyState = {
    there: "Saved",
    missing: "No key saved",
    environment: "From environment",
    "none needed": "—",
  };

  function modelButton(text, pressed) {
    const button = el("button", null, text);
    button.type = "button";
    button.addEventListener("click", pressed);
    return button;
  }

  function modelInput(label, type = "text", required = true) {
    const field = el("label", null, label);
    const input = el("input");
    input.type = type;
    input.required = required;
    if (type === "password") input.autocomplete = "off";
    field.append(input);
    return [field, input];
  }

  function modelForm(fields, label, submit, close, notice) {
    const form = el("form", "model-server-form");
    const save = el("button", "primary", label);
    save.type = "submit";
    const status = el("p", "error model-status");
    status.setAttribute("role", "alert");
    const buttons = el("div", "model-actions");
    buttons.append(save, modelButton("Cancel", close));
    if (notice) form.append(el("p", "sub", notice));
    form.append(...fields, buttons, status);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (save.disabled) return;
      save.disabled = true;
      status.replaceChildren();
      try { await submit(); }
      catch (error) {
        if (error.modelFailure) status.append(modelFailureView(error.modelFailure));
        else status.textContent = error.message;
      }
      finally { save.disabled = false; }
    });
    return form;
  }

  function modelKeys(server, editor, refresh, close) {
    const [field, key] = modelInput("API key", "password");
    editor.replaceChildren(modelForm([field], "Save", async () => {
      const name = server.name;
      const version = nextModelVersion(name);
      const reply = await change("model/server/key/set", { name, key: key.value });
      key.value = "";
      if (!acceptModelDirectory(name, version, reply.directory)) return;
      const saved = `Saved key for ${server.title}.`;
      const directory = reply.directory;
      const message = directory?.error
        ? `Key saved. Couldn’t load ${server.title}’s model list. `
          + "Choose View models to retry."
        : saved + (directory && !directory.models.length ? " No models listed." : "");
      await refresh(message, directory?.error);
    }, close ?? (() => editor.replaceChildren())));
    key.focus();
  }

  function removeModelKey(server, editor, refresh) {
    editor.replaceChildren(modelForm([], "Remove saved key", async () => {
      const name = server.name;
      const version = nextModelVersion(name);
      const reply = await change("model/server/key/set", { name, key: null });
      if (!acceptModelDirectory(name, version, reply.directory)) return;
      await refresh(`Removed saved key for ${server.title}.`);
    }, () => editor.replaceChildren(), `Remove the saved key for ${server.title}?`));
  }

  async function showModelNames(server, editor, status) {
    const waiting = el("p", "sub", "Requesting the model list…");
    status.textContent = "";
    editor.replaceChildren(waiting);
    const name = server.name;
    const version = nextModelVersion(name);
    try {
      const reply = await look("model/list", { server: name });
      const row = reply.servers.find((item) => item.server === name);
      if (!acceptModelDirectory(name, version, row)) return;
      if (!waiting.isConnected) return;
      if (row?.error) {
        editor.replaceChildren(el("p", "error", row.error));
        return;
      }
      const list = el("ul", "model-names");
      const models = row?.models || [];
      for (const model of models) {
        list.append(el("li", null, model.slice(server.model_prefix?.length || 0)));
      }
      editor.replaceChildren(models.length
        ? list : el("p", "sub", "No models listed."));
    } catch (error) {
      if (waiting.isConnected) {
        editor.replaceChildren(el("p", "error", error.message));
      }
    }
  }

  function modelNameInput(server) {
    const [field, model] = modelInput("Model name");
    const known = el("datalist");
    known.id = "known-models";
    const listed = state.modelDirectories.get(server.name)?.models || [];
    const ids = listed.map((model) => model.slice(server.model_prefix?.length || 0));
    for (const name of new Set([...ids, ...Object.keys(server.models || {})])) {
      const option = el("option");
      option.value = name;
      known.append(option);
    }
    model.setAttribute("list", known.id);
    field.append(known);
    return [field, model];
  }

  function modelTest(server, editor, status) {
    const [field, model] = modelNameInput(server);
    editor.replaceChildren(modelForm([field], "Run test", async () => {
      const reply = await change("model/server/test", {
        name: server.name, model: model.value.trim(),
      });
      status.textContent = `${reply.model} answered the test.`;
      editor.replaceChildren();
    }, () => editor.replaceChildren(), "The provider may charge for the test."));
    model.focus();
  }

  function connectModel(server, editor, refresh, close) {
    const [nameField, name] = modelInput("Server name");
    const [urlField, url] = modelInput("API URL", "url");
    const [modelField, model] = modelNameInput(server);
    const [contextField, tokens] = modelInput("Context window in tokens",
      "number", false);
    tokens.min = "1";
    const [keyField, key] = modelInput("API key", "password", false);
    const fields = [modelField, contextField];
    if (!server.name) fields.unshift(nameField, urlField);
    if (!server.name || server.key_state === "missing") fields.push(keyField);
    editor.replaceChildren(modelForm(fields, "Test and connect", async () => {
      const values = {
        name: server.name || name.value.trim(), model: model.value.trim(),
      };
      if (!server.name) values.url = url.value.trim();
      if (key.value) values.key = key.value;
      if (tokens.value) values.context_window = Number(tokens.value);
      const reply = await change("model/server/connect", values);
      key.value = "";
      state.modelServer = values.name;
      if (reply.default) {
        const setting = state.settings.find((row) => row.name === "model");
        if (setting) setting.value = reply.model;
      }
      await refresh(`Connected ${reply.model}.`);
    }, close, "Connecting makes a short test the provider may charge for."));
    (server.name ? model : name).focus();
  }

  async function modelServers(body, notice = "", directoryError = "") {
    const version = ++state.modelPanelVersion;
    const reply = await look("model/server/list");
    if (version !== state.modelPanelVersion || state.settingsView !== "Models"
        || !$("settings-dialog").open) return;
    const refresh = (status, error) => modelServers(body, status, error);
    draw(notice, false, directoryError);

    function draw(message, addKey = false, directoryError = "") {
      const status = el("div", "sub model-status", message);
      status.setAttribute("role", "status");
      if (directoryError) status.append(failureDetails([directoryError]));
      body.replaceChildren(status);

      const table = el("table", "model-server-list");
      const heading = table.createTHead().insertRow();
      for (const name of ["Model server", "API key", "Actions"]) {
        const cell = el("th", null, name);
        cell.scope = "col";
        heading.append(cell);
      }
      const rows = table.createTBody();
      for (const server of reply.servers) {
        const row = rows.insertRow();
        row.dataset.modelServer = server.name;
        row.insertCell().textContent = server.title || server.name;
        row.insertCell().textContent = modelKeyState[server.key_state] || "—";
        const missing = server.key_state === "missing";
        const action = modelButton(missing ? "Add API key" : "Manage", () => {
          state.modelServer = server.name;
          draw("", missing);
        });
        action.setAttribute("aria-label", `${action.textContent} for ${server.title}`);
        row.insertCell().append(action);
      }
      body.append(table);

      const close = () => {
        state.modelServer = null;
        draw("");
      };
      const selected = reply.servers.find((row) => row.name === state.modelServer);
      if (selected) {
        const panel = el("section", "model-server-panel");
        const editor = el("div", "model-server-editor");
        panel.append(el("h3", null, selected.title),
          el("p", "sub model-server-url", selected.url));
        if (addKey) {
          panel.append(editor);
          body.append(panel);
          modelKeys(selected, editor, refresh, close);
        } else {
          const actions = el("div", "model-actions");
          const saved = selected.has_saved_key ?? selected.key_state === "there";
          const usesKey = selected.needs_key || saved
            || selected.key_state === "environment";
          if (usesKey) {
            actions.append(modelButton(saved ? "Replace saved key" : "Add API key",
              () => modelKeys(selected, editor, refresh)));
          }
          if (saved) actions.append(modelButton("Remove saved key",
            () => removeModelKey(selected, editor, refresh)));
          actions.append(
            modelButton("View models", () => showModelNames(selected, editor, status)),
            modelButton("Test model", () => modelTest(selected, editor, status)),
            modelButton("Connect a model", () => connectModel(
              selected, editor, refresh, () => editor.replaceChildren())),
          );
          if (selected.user_added) {
            actions.append(modelButton("Remove server", () => {
              editor.replaceChildren(modelForm([], "Remove server", async () => {
                const name = selected.name;
                const version = nextModelVersion(name);
                await change("model/server/disconnect", { name });
                if (!acceptModelDirectory(name, version)) return;
                state.modelServer = null;
                await refresh(`Removed ${selected.title}.`);
              }, () => editor.replaceChildren(), `Remove ${selected.title}?`));
            }));
          }
          actions.append(modelButton("Close", close));
          panel.append(actions, editor);
          body.append(panel);
          actions.querySelector("button")?.focus();
        }
      } else if (state.modelServer === "") {
        const editor = el("section", "model-server-panel");
        body.append(editor);
        connectModel({}, editor, refresh, close);
      } else {
        body.append(modelButton("Add model server", () => {
          state.modelServer = "";
          draw("");
        }));
      }
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
      for (const server of listed.servers) {
        const prefix = server.model_prefix ?? `${server.name}/`;
        const models = Object.keys(server.models || {}).map((model) => prefix + model);
        state.modelDirectories.set(server.name, { models });
      }
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
    } else { connect(); drawDetails(); }
    if (!state.open
        && (state.launch.view === "new" || (!target && state.launch.workspace
        && state.launch.view !== "sessions"))) openNew();
    $("new-session").disabled = false;
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
      icon.href = `/ui/favicon-${theme}${still}.svg`;
    });
  }
  darkScheme.addEventListener("change", applyTheme);
  reducedMotion.addEventListener("change", applyTheme);
  applyTheme();
  start();
})();
