/* Protocol 8 timeline rendering, block-memoized draft adoption, and clean folds. */

import { look } from "./api.js";
import { registry } from "./components.js";
import {
  $, GAP_SECONDS, PAGE_RECORDS, RESULT_CHARS, clock, el, formatBytes,
  modelFailureView, seconds, sourceWho, state,
} from "./dom.js";
import * as entries from "./entries.js";
import { patchMarkdown } from "./markdown.js";

let hooks = {
  rewindMessage: () => {},
  drawAsk: () => {},
};

let activeAudio = null;
let activeListenButton = null;

function stopActiveAudio() {
  if (activeAudio) {
    try { activeAudio.pause(); } catch { /* ignore */ }
    activeAudio.src = "";
    activeAudio = null;
  }
  if (activeListenButton) {
    activeListenButton.textContent = "Listen";
    activeListenButton.setAttribute("aria-pressed", "false");
    activeListenButton = null;
  }
}

function playAttachmentAudio(attachmentId, session, button = null) {
  stopActiveAudio();
  const params = new URLSearchParams();
  if (session) params.set("session", session);
  const url = `/api/attachments/${encodeURIComponent(attachmentId)}`
    + (params.toString() ? `?${params}` : "");
  const audio = new Audio(url);
  activeAudio = audio;
  activeListenButton = button;
  if (button) {
    button.textContent = "Stop";
    button.setAttribute("aria-pressed", "true");
  }
  const clear = () => {
    if (activeAudio === audio) stopActiveAudio();
  };
  audio.addEventListener("ended", clear);
  audio.addEventListener("error", clear);
  const promise = audio.play();
  if (promise && typeof promise.catch === "function") {
    promise.catch(() => {
      clear();
    });
  }
}

async function toggleListen(turn, button) {
  if (activeListenButton === button && activeAudio) {
    stopActiveAudio();
    return;
  }
  const session = state.open;
  if (!session) return;
  const replyEntryId = turn.lastReplyId || null;
  button.disabled = true;
  button.textContent = "…";
  try {
    const response = await fetch("/api/speech", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session,
        ...(replyEntryId ? { reply_entry_id: replyEntryId } : {}),
      }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok || !body.ok || !body.attachment_id) {
      throw new Error(body.error || `HTTP ${response.status}`);
    }
    button.disabled = false;
    playAttachmentAudio(body.attachment_id, session, button);
  } catch (error) {
    button.disabled = false;
    button.textContent = "Listen";
    alert(error.message);
  }
}

export function initTimeline(callbacks) {
  hooks = { ...hooks, ...callbacks };
  $("timeline").addEventListener("scroll", () => {
    if (atEnd()) $("follow").hidden = true;
  });
  $("follow").addEventListener("click", scrollToEnd);
}

export function atEnd() {
  const timeline = $("timeline");
  return timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 80;
}

export function scrollToEnd() {
  const timeline = $("timeline");
  timeline.scrollTop = timeline.scrollHeight;
  $("follow").hidden = true;
}

export function earlierButton() {
  const button = el("button", "earlier ghost", `Earlier · ${state.earlier}`);
  button.type = "button";
  button.addEventListener("click", loadEarlier);
  return button;
}

export async function loadEarlier() {
  const first = state.records.length ? state.records[0].seq : null;
  if (first === null) return;
  const id = state.open;
  const reply = await look("session/read", {
    session: id, before: first, limit: PAGE_RECORDS,
  });
  if (state.open !== id) return;
  const expanded = new Set(Array.from($("timeline").querySelectorAll(
    ".process[open] [data-seq]",
  )).map((node) => node.dataset.seq));
  const folds = new Set(Array.from($("timeline").querySelectorAll(
    "details[open][data-fold]",
  )).map((node) => node.dataset.fold));
  const calls = new Set(Array.from($("timeline").querySelectorAll(
    "details[open][data-call]",
  )).map((node) => node.dataset.call));
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
  hooks.drawAsk();
  timeline.scrollTop = timeline.scrollHeight - height;
}

export function addRecord(record, live = true) {
  if (record.seq <= state.lastSeq) return;
  const following = live && atEnd();
  state.lastSeq = record.seq;
  state.records.push(record);
  const kind = entries.entryType(record);
  if (kind === "media/speech_cache") {
    if (live && entries.field(record, "auto_speak") && entries.field(record, "attachment_id")) {
      playAttachmentAudio(entries.field(record, "attachment_id"), state.open);
    }
    return;
  }
  if (entries.UNDRAWN_TYPES.has(kind) || record.audience === "model") return;
  if (entries.isTurnStart(record)) {
    if (state.turn) finishProgress(state.turn, "Progress");
    state.turnStarted = record.time;
  }
  if (kind !== "chat/assistant_reply") clearDraft();
  const timeline = $("timeline");
  if (record.time) {
    const at = new Date(record.time).getTime() / 1000;
    if (!state.turn && state.lastTime !== null
        && at - state.lastTime > GAP_SECONDS) {
      timeline.append(el("div", "divider", `${seconds(at - state.lastTime)} later`));
    }
    state.lastTime = at;
  }
  const node = entryFor(record);
  if (node) {
    const isNotice = entries.isNotice(record);
    const isSteer = Boolean(entries.field(record, "steer"));
    if (entries.isTurnInput(record) && !isNotice && !isSteer) {
      timeline.append(node);
      turnFor(record);
    } else if (kind === "sessions/answer" || entries.isTurnInput(record)) {
      const turn = turnFor(record);
      if (isNotice) {
        turn.hasProcess = true;
        turn.process.append(node);
      } else {
        turn.replies.append(node);
      }
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
  const kind = entries.entryType(record);
  const fields = entries.fieldsOf(record);
  const custom = registry.getEntry(kind);
  if (custom) return custom(record, { el, state, entries, who });
  if (entries.isTurnInput(record)) return userEntry(record);
  if (entries.NOTE_TYPES.has(kind)) {
    return entries.modelFailure(record)
      ? modelFailureEntry(record) : noteEntry(record);
  }
  switch (kind) {
    case "chat/assistant_reply": return assistantEntry(record);
    case "tools/result": return toolEntry(record);
    case "sessions/ask": return askEntry(record);
    case "sessions/answer": return answerEntry(record);
    case "sessions/turn_end": return turnEnd(record);
    case "sessions/retry": return modelFailureEntry(record);
    case "sessions/rewind_note": return el("div", "divider", "Rewound");
    case "sessions/switch_note": return el("div", "divider", switchText(record));
    case "sessions/header":
      return el("div", "divider", fields.note || "Started");
    default: return null;
  }
}

function switchText(record) {
  const to = entries.field(record, "to") || {};
  const parts = Object.keys(to).map((name) => `${name} ${to[name]}`);
  return parts.length ? parts.join(" · ") : "Switched";
}

function renderAttachmentFallback(item) {
  const id = item?.attachment_id || "";
  const path = item?.path || "";
  if (!id && !path) return null;
  const card = el("div", "attachment-card");
  if (id) card.dataset.attachmentId = id;
  else card.dataset.attachmentPath = path;
  const info = el("div", "attachment-card-info");
  info.append(el("span", "attachment-card-name", item.name || "file"));
  if (!id) {
    const mediaType = String(item.media_type || "").split(";")[0].trim().toLowerCase();
    const sizeText = item.size_bytes ? formatBytes(item.size_bytes) : "";
    const meta = [mediaType, sizeText, path].filter(Boolean).join(" · ");
    if (meta) info.append(el("span", "attachment-card-meta", meta));
    card.append(info);
    return card;
  }
  card.append(info);
  const params = new URLSearchParams({ download: "1" });
  if (state.open) params.set("session", state.open);
  const link = el("a", "attachment-download", "Download");
  link.href = `/api/attachments/${encodeURIComponent(id)}?${params}`;
  link.download = item.name || "file";
  card.append(link);
  return card;
}

function renderAttachments(record) {
  const items = entries.attachmentsOf(record);
  if (!items.length) return null;
  const wrap = el("div", "entry-attachments");
  const renderer = registry.getEntry(entries.ATTACHMENT_TYPE);
  for (const item of items) {
    const node = renderer
      ? renderer(item, { el, state, entries, session: state.open })
      : renderAttachmentFallback(item);
    if (node) wrap.append(node);
  }
  return wrap.children.length ? wrap : null;
}

function renderSearchSuggestions(record) {
  const suggestions = entries.searchSuggestions(record);
  if (!suggestions) return null;
  const wrap = el("div", "search-suggestions-wrap");
  if (suggestions.html) {
    const iframe = el("iframe", "search-suggestions");
    iframe.setAttribute("sandbox", "allow-popups allow-popups-to-escape-sandbox");
    iframe.setAttribute("title", "Search suggestions");
    iframe.setAttribute("referrerpolicy", "no-referrer");
    iframe.srcdoc = suggestions.html;
    wrap.append(iframe);
  } else if (suggestions.queries.length) {
    const list = el("div", "search-queries");
    for (const query of suggestions.queries) {
      list.append(el("span", "search-query", query));
    }
    wrap.append(list);
  }
  return wrap.children.length ? wrap : null;
}

export function userEntry(record) {
  const fields = entries.fieldsOf(record);
  const text = entries.messageText(record);
  const attachmentsNode = renderAttachments(record);
  const event = entries.interaction(record);
  const node = el("div", "entry you");
  node.dataset.seq = record.seq;
  if (entries.isNotice(record)) {
    node.className = "entry notice";
    const name = record.notice_source || "Background notice";
    node.append(who(event?.sender_session ? record.sender_name : name, record));
    if (text) node.append(el("div", "bubble", text));
    if (attachmentsNode) node.append(attachmentsNode);
    for (const action of registry.toolActions()) {
      const control = action(record, node);
      if (control) node.append(control);
    }
    return node;
  }
  const isCont = entries.isContinue(record);
  const more = event?.action === "queue" ? "queued"
    : fields.steer ? "steer" : (isCont ? "go on" : "");
  node.append(who(record.sender_name || sourceWho(fields.source), record, more));
  if (isCont && !text && !attachmentsNode) return node;
  if (text || !attachmentsNode) node.append(el("div", "bubble", text));
  if (attachmentsNode) node.append(attachmentsNode);
  if (state.rewindPoints.has(record.seq)) {
    const session = state.open;
    const button = el("button", "message-action", "Rewind and edit");
    button.type = "button";
    button.addEventListener("click", () => hooks.rewindMessage(session, record.seq));
    node.append(button);
  }
  return node;
}

function turnFor(record) {
  if (state.turn) return state.turn;
  const node = el("article", "entry zero turn");
  const isNotice = entries.isNotice(record);
  node.append(who("ZERO", { time: state.turnStarted || record?.time },
    isNotice ? `Following up on ${record.notice_source || "Background notice"}` : ""));
  const process = el("details", "process");
  const summary = el("summary", null, "Thinking…");
  const liveStep = el("div", "process-step live-step", "Thinking");
  process.append(summary, liveStep);
  const progress = el("div", "turn-progress active");
  progress.append(process);
  const replies = el("div", "turn-replies");
  const actions = el("div", "turn-actions");
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
  const listen = el("button", "message-action listen-button", "Listen");
  listen.type = "button";
  listen.hidden = true;
  listen.setAttribute("aria-pressed", "false");
  actions.append(copy, listen);
  node.append(replies, progress, actions);
  $("timeline").append(node);
  const turn = {
    node, progress, process, summary, replies, copy, listen, texts, liveStep,
    lastReplyId: null,
    phase: "Thinking", hasProcess: false,
  };
  state.turn = turn;
  listen.addEventListener("click", () => toggleListen(turn, listen));
  return turn;
}

function assistantEntry(record) {
  const fields = entries.fieldsOf(record);
  const reasoning = entries.assistantReasoning(record);
  const calls = entries.toolCalls(record);
  const content = entries.assistantText(record);
  const turn = turnFor(record);
  turn.liveStep.remove();
  if (reasoning || calls.length) {
    turn.hasProcess = true;
    const node = el("div", "process-step");
    node.dataset.seq = record.seq;
    turn.process.append(node);
    if (reasoning) {
      const fold = el("details", "thinking");
      fold.dataset.fold = `thinking-${record.seq}`;
      fold.append(el("summary", null, "Thinking"), el("div", "body", reasoning));
      node.append(fold);
    }
    for (const call of calls) {
      node.append(workEntry(
        entries.callId(call), entries.callName(call), entries.callArguments(call),
      ));
    }
  }
  const replyFields = fields.reply;
  const text = content || [
    replyFields?.answer, replyFields?.why, replyFields?.next?.user,
    replyFields?.next?.zero,
  ].filter((value) => value?.trim()).join("\n\n");
  const draftNode = $("draft");
  if (text.trim()) {
    let replyNode;
    if (draftNode && draftNode.parentNode === turn.replies) {
      // Adopt #draft in place without reparenting so text selection survives.
      draftNode.removeAttribute("id");
      draftNode.classList.remove("draft");
      draftNode.hidden = false;
      state.draft = null;
      replyNode = draftNode;
    } else {
      clearDraft();
      replyNode = el("div", "public-reply");
      turn.replies.append(replyNode);
    }
    drawPublicReply(replyNode, replyFields, text, record.seq);
    turn.texts.push(text);
    const replyId = entries.entryOf(record).id || null;
    if (replyId) {
      turn.lastReplyId = replyId;
      turn.listen.dataset.replyId = replyId;
    }
    turn.copy.hidden = false;
    turn.listen.hidden = false;
  } else {
    clearDraft();
  }
  const attachmentsNode = renderAttachments(record);
  if (attachmentsNode) turn.replies.append(attachmentsNode);
  showProgress(turn, calls.length ? "Working" : "Thinking");
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

function toolEntry(record) {
  const fields = entries.fieldsOf(record);
  const call = entries.toolCallId(record);
  let fold = state.calls.get(call);
  if (!fold) {
    // The history window can start between a call and its result.
    fold = workEntry(call, fields.tool, fields.arguments);
    const step = el("div", "process-step");
    step.dataset.seq = record.seq;
    step.append(fold);
    const turn = turnFor(record);
    turn.hasProcess = true;
    turn.liveStep.remove();
    showProgress(turn, "Working");
    turn.process.append(step);
  }
  fold.querySelector(".body").textContent = fields.denied ? "Declined"
    : fields.failed || fields.bad_call ? "Failed" : "Finished";
  const result = fields.result || "";
  if (result) {
    const output = result.length > RESULT_CHARS
      ? result.slice(0, RESULT_CHARS) + `\n… +${result.length - RESULT_CHARS}`
      : result;
    fold.append(el("pre", null, output));
  }
  if (fields.saved_path) fold.append(el("div", "saved", fields.saved_path));
  const attachmentsNode = renderAttachments(record);
  if (attachmentsNode) {
    const items = entries.attachmentsOf(record);
    const isDeliverable = fields.tool === "present_file"
      || items.some((item) => item.delivery === "reference");
    if (isDeliverable) {
      attachmentsNode.dataset.tool = fields.tool || "deliverable";
      turnFor(record).replies.append(attachmentsNode);
    } else {
      fold.append(attachmentsNode);
    }
  }
  const suggestionsNode = renderSearchSuggestions(record);
  if (suggestionsNode) {
    suggestionsNode.dataset.tool = fields.tool || "web_search";
    turnFor(record).replies.append(suggestionsNode);
  }
  for (const action of registry.toolActions()) {
    const node = action(record, fold);
    if (node) fold.append(node);
  }
  return null;
}

function askEntry(record) {
  const turn = turnFor(record);
  turn.hasProcess = true;
  showProgress(turn, turn.phase, true);
  return null;
}

function answerEntry(record) {
  const fields = entries.fieldsOf(record);
  const turn = turnFor(record);
  showProgress(turn, turn.phase, false);
  const node = el("div", "entry you");
  const event = entries.interaction(record);
  const action = { approve: "approved", decline: "declined" }[event?.action]
    || "answered";
  const target = event?.target?.ask || entries.askId(record);
  node.append(who(record.sender_name || sourceWho(fields.source), record,
    [action, target].filter(Boolean).join(" · ")));
  if (fields.question_text) {
    node.append(el("div", "meta", fields.question_text));
  }
  const words = fields.text || fields.choice || (fields.yes ? "yes" : "no");
  node.append(el("div", "bubble", words));
  return node;
}

function turnEnd(record) {
  const fields = entries.fieldsOf(record);
  const failure = entries.modelFailure(record);
  const failed = fields.outcome === "failed" || fields.error;
  const text = [fields.outcome, seconds(fields.seconds),
    fields.error || fields.detail || ""].filter(Boolean).join(" · ");
  const turn = turnFor(record);
  if (failure) {
    turn.hasProcess = true;
    turn.replies.append(modelFailureView(failure));
  }
  if (!turn.texts.length && fields.answer) {
    const reply = el("div", "public-reply");
    drawPublicReply(reply, fields.reply, fields.answer, record.seq);
    turn.replies.append(reply);
    turn.texts.push(fields.answer);
    turn.copy.hidden = false;
    turn.listen.hidden = false;
  }
  finishProgress(
    turn,
    failure ? `Stopped · ${seconds(fields.seconds)}` : text || "Completed",
    { outcome: fields.outcome, hasFailure: Boolean(failure) },
  );
  if (failed) turn.summary.classList.add("failed");
  return null;
}

function modelFailureEntry(record) {
  const failure = entries.modelFailure(record);
  if (failure) {
    const turn = turnFor(record);
    turn.hasProcess = true;
    turn.replies.append(modelFailureView(failure));
  }
  return null;
}

export function showProgress(turn, phase,
                             waiting = turn.progress.classList.contains("waiting")) {
  turn.phase = phase;
  turn.progress.classList.toggle("waiting", waiting);
  const label = waiting ? "Waiting for you" : phase + "…";
  if (turn.summary.textContent !== label) turn.summary.textContent = label;
}

export function finishProgress(turn, label, options = {}) {
  turn.liveStep.remove();
  for (const fold of state.calls.values()) {
    const status = fold.querySelector(".body");
    if (status.textContent === "In progress") status.textContent = "No result recorded";
  }
  const omitBlank = options.outcome === "answered"
    && !turn.hasProcess && !options.hasFailure;
  turn.progress.classList.remove("active", "waiting");
  if (omitBlank) {
    turn.progress.remove();
  } else {
    turn.summary.textContent = label;
    turn.node.insertBefore(turn.progress, turn.replies);
  }
  state.turn = null;
  state.calls.clear();
}

function noteEntry(record) {
  const fields = entries.fieldsOf(record);
  const kind = fields.kind || "note";
  const name = kind.startsWith("job") ? "Job"
    : kind.startsWith("round") ? "Rounds"
    : kind.startsWith("subagent") ? "Subagent" : "ZERO";
  const node = el("div", "entry");
  node.append(who(name, record, kind.replace(/_/g, " ")));
  node.append(el("div", "notice", fields.text || ""));
  return node;
}

export function drawDraft(draft) {
  state.draft = draft;
  const turn = turnFor({});
  const following = atEnd();
  let node = $("draft");
  if (!node) {
    node = el("div", "public-reply draft");
    node.id = "draft";
  }
  const text = draft.reply && !draft.reply.answer?.trim() ? "" : draft.text;
  drawPublicReply(node, draft.reply, text, state.lastSeq + 1);
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
    const values = fields.filter((value) => value?.trim());
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
      let wrap = body.children[index];
      if (!wrap || !wrap.classList.contains("reply")) {
        wrap = el("div", "reply");
        if (body.children[index]) body.children[index].replaceWith(wrap);
        else body.append(wrap);
      }
      if (wrap.sourceText === value) return;
      patchMarkdown(wrap, value);
      wrap.sourceText = value;
    });
    while (body.children.length > values.length) body.lastElementChild.remove();
  }
}

export function clearDraft() {
  state.draft = null;
  const old = $("draft");
  if (old) old.remove();
}

export function lastAssistantText(records) {
  for (let i = records.length - 1; i >= 0; i -= 1) {
    const record = records[i];
    if (entries.entryType(record) === "chat/assistant_reply") {
      const text = entries.assistantText(record);
      if (text) return text;
    }
  }
  return "";
}
