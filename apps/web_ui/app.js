/* ZERO's web UI entrypoint: SSE stream, session lifecycle, layout, and startup.
 *
 * Every request is one protocol message, whole (POST /api/request); one
 * stream (GET /api/events) brings the session list (opening snapshot +
 * incremental upsert/remove diffs) and the open session's Protocol 8 Entry
 * events after the last seq the page holds. Every node is built with
 * textContent: nothing a session holds is ever read as HTML.
 */

import { change, look, pairBrowser, setWakeHandler } from "./api.js";
import { drawAsk } from "./ask.js";
import { createHost, loadComponents } from "./components.js";
import {
  checkDeliveries, historyActions, initComposer, restoreComposerDraft,
  restoreWithdrawn, rewindMessage, saveComposerDraft, saveSubmissions,
  sizeComposer,
} from "./composer.js";
import {
  acceptModelDirectory, drawDetails, ensureModelWindow, initDetails,
  nextModelVersion,
} from "./details.js";
import {
  $, HIDDEN_SECONDS, PAGE_RECORDS, WIDTHS, actionDialog, applyTheme, el,
  failureDetails, modelFailureView, rowOf, showPanel, state, tokens,
} from "./dom.js";
import * as entries from "./entries.js";
import { applyListEvent, drawSessions, initSessions } from "./sessions.js";
import { initSettings, openSettings } from "./settings.js";
import {
  addRecord, clearDraft, drawDraft, earlierButton, finishProgress,
  initTimeline, scrollToEnd, showProgress, userEntry,
} from "./timeline.js";

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
    if (applyListEvent(data)) {
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
    restoreWithdrawn(state.open, event.withdrawn.map((item) => item.id));
    return;
  }
  if (event.record) {
    const inbox = entries.field(event.record, "inbox");
    if (inbox && state.submissions.delete(inbox)) saveSubmissions();
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
      else finishProgress(state.turn, "Completed", { outcome: "completed" });
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
  state.settingUpdates = new Set();
  state.sessionRead = null;
  state.settingsStatus = "";
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
  state.sessionRead = Promise.all([
    look("session/read", { session: id, limit: PAGE_RECORDS }),
    look("session/user_messages", { session: id }),
  ]);
  try {
    const [reply, points] = await state.sessionRead;
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
  state.settingUpdates = new Set();
  state.sessionRead = null;
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

function drawHead() {
  const row = state.newSession || rowOf(state.open);
  $("session-title").textContent = row ? row.title || row.id : state.open || "";
  $("session-title").disabled = !row;
}

async function markSeen() {
  const row = rowOf(state.open);
  if (!row || !state.lastSeq || state.lastSeq <= state.seenSent) return;
  if (!row.unseen) return;
  state.seenSent = state.lastSeq;
  try {
    await change("session/mark_seen", { session: state.open, seq: state.lastSeq });
  } catch {
    state.seenSent = 0;
  }
}

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

function openNew() {
  const defaults = Object.fromEntries(state.settings.map((s) => [s.name, s.value]));
  const { workspace, mode, model, thinking_effort } = state.launch;
  state.firstSend = null;
  openSession(null, {
    title: "New session",
    workspace: workspace || "/",
    mode: mode || defaults.mode || state.hello?.mode || "default",
    model: model || defaults.model || state.hello?.model,
    thinking_effort: thinking_effort || defaults.thinking_effort || undefined,
  });
}

function stored(name) {
  try {
    return Number(localStorage.getItem(`zero.width.${name}`)) || null;
  } catch {
    return null;
  }
}

function setWidth(name, value) {
  const [, low, high] = WIDTHS[name];
  const width = Math.max(low, Math.min(high, value));
  document.documentElement.style.setProperty(`--${name}`, `${width}px`);
  try {
    localStorage.setItem(`zero.width.${name}`, String(width));
  } catch {
    // A private window keeps no widths
  }
}

function bindEvents() {
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
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === "Escape") {
        event.preventDefault();
        finish(event.key === "Enter", true);
      }
    });
  });

  $("new-session").addEventListener("click", openNew);
  $("back").addEventListener("click", () => showPanel("sessions"));
  $("details-toggle").addEventListener("click", () => {
    const frame = $("frame");
    if (window.matchMedia("(max-width: 760px)").matches) {
      showPanel(frame.classList.contains("show-details") ? "session" : "details");
      return;
    }
    frame.classList.toggle("no-details");
  });

  for (const handle of document.querySelectorAll(".handle")) {
    const name = handle.dataset.handle;
    const width = stored(name);
    if (width) setWidth(name, width);
    handle.addEventListener("dblclick", () => setWidth(name, WIDTHS[name][0]));
    handle.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      const move = (moved) => setWidth(
        name,
        name === "left" ? moved.clientX : window.innerWidth - moved.clientX,
      );
      const up = () => {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", up);
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", up);
    });
  }

  $("archived").addEventListener("click", () => {
    state.archived = !state.archived;
    $("archived").textContent = state.archived ? "Active sessions" : "Archived";
    $("archived").setAttribute("aria-label", $("archived").textContent);
    drawSessions();
  });
}

async function start() {
  setWakeHandler(wake);
  initSessions({ openSession, drawHead, historyActions });
  initTimeline({ rewindMessage, drawAsk });
  initComposer({
    openSession, openNew, openSettings, drawDetails, drawHead, ensureModelWindow,
  });
  initDetails({ openNew });
  initSettings();
  bindEvents();
  applyTheme();

  const host = createHost({
    api: { change, look },
    dom: {
      $, el, rowOf, showPanel, state, actionDialog, tokens,
      failureDetails, modelFailureView,
    },
    entries,
    models: { acceptModelDirectory, nextModelVersion },
    openSession,
    openNew,
    openSettings,
    drawDetails,
    drawAsk,
    drawSessions,
  });
  await loadComponents(host);

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
  applyListEvent(listed);
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
  } else {
    connect();
    drawDetails();
  }
  if (!state.open
      && (state.launch.view === "new" || (!target && state.launch.workspace
      && state.launch.view !== "sessions"))) {
    openNew();
  }
  $("new-session").disabled = false;
  sizeComposer();
}

start();
