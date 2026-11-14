/* ZERO's web UI: the sessions, one session's timeline, and its details,
 * all from ZERO's server through the HTTP door; the page keeps nothing of its
 * own but where the panels' edges are.
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
  const WIDTHS = { left: [280, 200, 460], right: [340, 260, 640] };

  const state = {
    hello: null, sessions: [], open: null, records: [], earlier: 0,
    runsWith: null, context: null, events: null, asleep: false,
    calls: new Map(), lastSeq: 0, lastTime: null, draft: null,
    hiddenTimer: null, seenSent: 0,
  };

  // ------------------------------------------------------------ requests

  function newId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return String(Date.now()) + Math.random().toString(16).slice(2);
  }

  async function post(payload) {
    const reply = await fetch("/api/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      credentials: "same-origin",
    });
    if (reply.status === 401) {
      location.reload();
      throw new Error("signed out");
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
      if (error.message === "signed out") throw error;
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
    events.addEventListener("session", (event) => sessionEvent(
      JSON.parse(event.data)));
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
    const words = $("search").value.trim().toLowerCase();
    const rows = state.sessions.slice().sort(
      (a, b) => (b.updated_at || 0) - (a.updated_at || 0));
    const ids = new Set(rows.map((row) => row.id));
    const children = new Map();
    for (const row of rows) {
      if (row.parent && ids.has(row.parent)) {
        if (!children.has(row.parent)) children.set(row.parent, []);
        children.get(row.parent).push(row);
      }
    }
    const matches = (row) => !words || [row.title, row.workspace,
      stateWord(row)].join(" ").toLowerCase().includes(words);
    // The ones that wait for you first, then the rest by their last change
    const tops = rows.filter((row) => !row.parent || !ids.has(row.parent));
    tops.sort((a, b) => (stateWord(b) === "waiting") - (stateWord(a) === "waiting"));
    list.replaceChildren();
    const add = (row, depth) => {
      const kids = children.get(row.id) || [];
      if (matches(row) || kids.some(matches)) {
        list.append(sessionRow(row, depth, kids.length));
      }
      if (depth < 1 || row.id === state.open || kids.some(
        (kid) => kid.id === state.open)) {
        for (const kid of kids) add(kid, depth + 1);
      }
    };
    for (const row of tops) add(row, 0);
    if (!list.children.length) {
      list.append(el("li", "loading", "—"));
    }
  }

  function sessionRow(row, depth, kids) {
    const item = el("li", "row" + (depth ? " child" : "")
      + (row.id === state.open ? " open" : ""));
    item.tabIndex = 0;
    item.style.marginLeft = depth ? `${16 * depth}px` : "";
    const dot = el("span", "dot" + (row.unseen ? " unseen" : ""));
    const title = el("span", "title", row.title || "(untitled)");
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
    item.addEventListener("click", () => openSession(row.id));
    item.addEventListener("keydown", (event) => {
      if (event.key === "Enter") openSession(row.id);
    });
    return item;
  }

  function rowOf(id) {
    return state.sessions.find((row) => row.id === id) || null;
  }

  $("search").addEventListener("input", drawSessions);
  $("search").addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.target.value = "";
      drawSessions();
    }
  });

  // ------------------------------------------------------------ a session

  async function openSession(id) {
    state.open = id;
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
      const reply = await look("session/read", { session: id,
                                                 limit: PAGE_RECORDS });
      if (state.open !== id) return;
      state.earlier = reply.earlier_count;
      state.runsWith = reply.runs_with;
      state.context = reply.context;
      timeline.replaceChildren();
      if (state.earlier) timeline.append(earlierButton());
      for (const record of reply.records) addRecord(record, false);
      scrollToEnd();
    } catch (error) {
      timeline.replaceChildren(el("div", "loading", error.message));
    }
    drawDetails();
    drawAsk();
    connect();
    markSeen();
  }

  function closeSession() {
    state.open = null;
    $("session-title").textContent = "";
    $("session-sub").textContent = "";
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
    // Drawn again whole, the earlier ones first
    const records = reply.records.concat(state.records);
    state.earlier = reply.earlier_count;
    state.records = [];
    state.calls = new Map();
    state.lastTime = null;
    const timeline = $("timeline");
    const height = timeline.scrollHeight;
    timeline.replaceChildren();
    if (state.earlier) timeline.append(earlierButton());
    for (const record of records) addRecord(record, false);
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
    $("session-sub").textContent = row ? folderName(row.workspace) : "";
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
    if (UNDRAWN.has(record.type)) return;
    clearDraft();
    const timeline = $("timeline");
    if (record.time) {
      const at = new Date(record.time).getTime() / 1000;
      if (state.lastTime !== null && at - state.lastTime > GAP_SECONDS) {
        timeline.append(el("div", "divider",
          `${seconds(at - state.lastTime)} later`));
      }
      state.lastTime = at;
    }
    const node = entryFor(record);
    if (node) timeline.append(node);
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
    const more = record.steer ? "steer" : (record.continue ? "go on" : "");
    node.append(who(sourceWho(record.source), record, more));
    if (record.continue && !record.text) return node;
    node.append(el("div", "bubble", record.text || ""));
    return node;
  }

  function assistantEntry(record) {
    const message = record.message || {};
    const node = el("div", "entry zero");
    node.append(who("ZERO", record));
    if (message.reasoning_content) {
      const fold = el("details", "thinking");
      fold.append(el("summary", null, "Thinking"),
                  el("div", "body", message.reasoning_content));
      node.append(fold);
    }
    if (message.content) node.append(markdown(message.content));
    const summaries = record.call_summaries || {};
    for (const call of message.tool_calls || []) {
      const fn = call.function || {};
      const line = summaries[call.id] || `${fn.name}(${fn.arguments || ""})`;
      const fold = el("details", "call");
      fold.append(el("summary", null, line));
      fold.dataset.call = call.id;
      state.calls.set(call.id, fold);
      node.append(fold);
    }
    if (!message.content && !(message.tool_calls || []).length
        && !message.reasoning_content) return null;
    return node;
  }

  function toolResult(fold, record) {
    const summary = fold.querySelector("summary");
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
    const node = el("div", "entry zero");
    const alone = el("details", "call");
    alone.append(el("summary", null, `${record.tool} ${record.arguments || ""}`));
    toolResult(alone, record);
    node.append(alone);
    return node;
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
    return el("div", "turn-end" + (failed ? " failed" : ""), text);
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
    node.append(who("ZERO", null));
    if (draft.text) node.append(markdown(draft.text));
    else if (draft.reasoning) node.append(el("div", "sub", "…"));
    if ((draft.calls || []).length) {
      node.append(el("div", "sub", `${draft.calls.join(", ")} …`));
    }
    const following = atEnd();
    $("timeline").append(node);
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
    $("composer").hidden = Boolean(ask);
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
    if (!text || !state.open) return;
    const button = $("send");
    button.disabled = true;
    try {
      await change("session/send", {
        session: state.open, text, source: "web",
        when_busy: $("queue").checked ? "queue" : "steer",
      });
      $("text").value = "";
      $("queue").checked = false;
    } catch (error) {
      alert(error.message);
    } finally {
      button.disabled = false;
    }
  }

  $("composer").addEventListener("submit", send);
  $("text").addEventListener("keydown", (event) => {
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
    panel.replaceChildren();
    if (!row) return;
    panel.append(runsWithSection(row));
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
    if (row.jobs || row.rounds) panel.append(await workSection(row));
    panel.append(actionsSection(row));
  }

  function runsWithSection(row) {
    const section = el("section");
    section.append(el("h2", null, "Session"));
    const list = el("dl");
    const mode = el("select");
    mode.setAttribute("aria-label", "Mode");
    for (const each of (state.hello && state.hello.modes) || []) {
      const option = el("option", null, each.name);
      option.value = each.name;
      option.title = each.about;
      if (each.name === row.mode) option.selected = true;
      mode.append(option);
    }
    mode.addEventListener("change", async () => {
      try {
        await change("session/set", { session: row.id, mode: mode.value });
      } catch (error) {
        alert(error.message);
      }
    });
    const runs = state.runsWith || {};
    mode.title = modeAbout(row.mode);
    const pairs = [["Mode", mode], ["Model", row.model], ["Folder", row.workspace],
      ["Thinking", runs.thinking_effort || ""]];
    if (row.pending && row.pending.length) {
      pairs.push(["Next", row.pending.join(", ")]);
    }
    for (const [name, value] of pairs) {
      if (value === "" || value === undefined) continue;
      list.append(el("dt", null, name));
      const dd = el("dd");
      if (value instanceof Node) dd.append(value);
      else dd.textContent = value;
      list.append(dd);
    }
    section.append(list);
    return section;
  }

  function familySection(row) {
    // The whole family: up to its first session, then everything under it
    let top = row;
    const seen = new Set();
    while (top.parent && rowOf(top.parent) && !seen.has(top.id)) {
      seen.add(top.id);
      top = rowOf(top.parent);
    }
    const section = el("section");
    section.append(el("h2", null, "Family"));
    const kids = (id) => state.sessions.filter((each) => each.parent === id)
      .sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
    if (top === row && !kids(row.id).length) return null;
    const branch = (node) => {
      const item = el("li");
      const button = el("button", node.id === state.open ? "open" : "",
        node.title || node.id);
      button.type = "button";
      button.addEventListener("click", () => openSession(node.id));
      item.append(button);
      const kind = node.parent ? (node.started_by === "zero" ? "subagent" : "fork")
        : "first";
      item.append(el("span", "kind", node.parent ? `${kind} · ${stateWord(node)}`
        : stateWord(node)));
      const under = kids(node.id);
      if (under.length) {
        const list = el("ul");
        for (const kid of under) list.append(branch(kid));
        item.append(list);
      }
      return item;
    };
    const tree = el("ul", "tree");
    tree.append(branch(top));
    section.append(tree);
    return section;
  }

  async function workSection(row) {
    const section = el("section");
    section.append(el("h2", null, "Background"));
    const list = el("ul", "small-list");
    try {
      if (row.jobs) {
        const reply = await look("job/list", { session: row.id });
        for (const job of reply.jobs.filter((each) => each.state === "running")) {
          list.append(el("li", null, job.command));
        }
      }
      if (row.rounds) list.append(el("li", null, `rounds · ${row.rounds}`));
    } catch (error) {
      list.append(el("li", "error", error.message));
    }
    section.append(list);
    return section;
  }

  function actionsSection(row) {
    const section = el("section");
    const rename = el("button", null, "Rename");
    rename.type = "button";
    rename.addEventListener("click", async () => {
      const title = prompt("Title", row.title || "");
      if (title && title.trim()) {
        await change("session/rename", { session: row.id, title: title.trim() })
          .catch((error) => alert(error.message));
      }
    });
    const archive = el("button", null, row.archived ? "Unarchive" : "Archive");
    archive.type = "button";
    archive.addEventListener("click", async () => {
      await change(row.archived ? "session/unarchive" : "session/archive",
                   { session: row.id }).catch((error) => alert(error.message));
    });
    const wrap = el("div", "composer-row");
    wrap.append(rename, archive);
    section.append(wrap, el("div", "sub", row.id));
    return section;
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
    if (row && !$("new-folder").value) $("new-folder").value = row.workspace;
    $("new-error").hidden = true;
    dialog.showModal();
  }

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

  // ------------------------------------------------------------ start

  async function start() {
    showPanel("sessions");
    drawDetails();
    try {
      state.hello = await look("server/hello");
    } catch (error) {
      $("server-state").textContent = error.message;
    }
    drawServer();
    connect();
  }

  start();
})();
