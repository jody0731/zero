/* Composer, unsent drafts, submission tracking, slash commands, and rewind. */

import { change, look, modelCapabilities, newId } from "./api.js";
import { registry } from "./components.js";
import {
  $, UTILITY_CAPABILITIES, actionDialog, draftKey, effortProblem, el,
  formatBytes, rowOf, showPanel, state,
} from "./dom.js";
import { lastAssistantText } from "./timeline.js";

const composerWarnings = new Map();

let hooks = {
  openSession: () => {},
  openNew: () => {},
  openSettings: () => {},
  drawDetails: () => {},
  drawHead: () => {},
  ensureModelWindow: () => {},
};

export function getComposerAttachments(session = state.open) {
  return state.composerAttachments.get(draftKey(session)) || [];
}

export function setComposerAttachments(session, items) {
  const key = draftKey(session);
  const clean = Array.isArray(items) ? items.filter((item) => item?.attachment_id) : [];
  if (clean.length) state.composerAttachments.set(key, clean);
  else state.composerAttachments.delete(key);
  if (key === draftKey(state.open)) renderComposerAttachments();
}

export function addComposerAttachment(key, item) {
  if (!item?.attachment_id) return;
  const current = state.composerAttachments.get(key) || [];
  if (current.some((each) => each.attachment_id === item.attachment_id)) return;
  state.composerAttachments.set(key, [...current, item]);
  if (key === draftKey(state.open)) renderComposerAttachments();
}

export function removeComposerAttachment(key, id) {
  const current = state.composerAttachments.get(key) || [];
  const next = current.filter((each) => each.attachment_id !== id);
  if (next.length) state.composerAttachments.set(key, next);
  else state.composerAttachments.delete(key);
  if (key === draftKey(state.open)) renderComposerAttachments();
}

function addComposerWarning(key, node) {
  const list = composerWarnings.get(key) || [];
  list.push(node);
  composerWarnings.set(key, list);
  if (key === draftKey(state.open)) renderComposerAttachments();
}

function removeComposerWarning(key, node) {
  const list = (composerWarnings.get(key) || []).filter((each) => each !== node);
  if (list.length) composerWarnings.set(key, list);
  else composerWarnings.delete(key);
  if (key === draftKey(state.open)) renderComposerAttachments();
}

export function renderComposerAttachments() {
  const tray = $("composer-attachments");
  if (!tray) return;
  const key = draftKey(state.open);
  const items = state.composerAttachments.get(key) || [];
  const warnings = composerWarnings.get(key) || [];
  tray.replaceChildren();
  for (const item of items) {
    const chip = el("span", "attachment-chip");
    chip.dataset.attachmentId = item.attachment_id;
    chip.append(el("span", "attachment-chip-name", item.name || item.attachment_id));
    if (typeof item.size_bytes === "number" && item.size_bytes > 0) {
      chip.append(el("span", "attachment-chip-size", formatBytes(item.size_bytes)));
    }
    const remove = el("button", "attachment-chip-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${item.name || "attachment"}`);
    remove.addEventListener("click", () => {
      removeComposerAttachment(key, item.attachment_id);
    });
    chip.append(remove);
    tray.append(chip);
  }
  for (const warn of warnings) tray.append(warn);
  tray.hidden = !items.length && !warnings.length;
}

export async function hydrateComposerAttachments(session, ids) {
  if (!Array.isArray(ids) || !ids.length) return;
  const loaded = await Promise.all(ids.map(async (id) => {
    if (typeof id === "object" && id?.attachment_id) return id;
    try {
      const reply = await look("attachment/get", {
        attachment_id: id, ...(session ? { session } : {}),
      });
      return reply.attachment || { attachment_id: id, name: id, size_bytes: 0 };
    } catch {
      return { attachment_id: id, name: id, size_bytes: 0 };
    }
  }));
  const existing = getComposerAttachments(session);
  const seen = new Set(existing.map((item) => item.attachment_id));
  const merged = [...existing];
  for (const item of loaded) {
    if (!seen.has(item.attachment_id)) {
      seen.add(item.attachment_id);
      merged.push(item);
    }
  }
  setComposerAttachments(session, merged);
}

export function mountComposerPlugins() {
  const form = $("composer");
  const textarea = $("text");
  const controls = $("composer-controls");
  const tray = $("composer-attachments");
  if (!form || !textarea || !controls) return;
  const ctx = {
    form,
    textarea,
    controls,
    tray,
    currentDraftKey: () => draftKey(state.open),
    readDraft: readComposerDraft,
    writeDraft: writeComposerDraft,
    getAttachments: (session = state.open) => getComposerAttachments(session),
    addAttachment: addComposerAttachment,
    removeAttachment: removeComposerAttachment,
    addWarning: addComposerWarning,
    removeWarning: removeComposerWarning,
    renderAttachments: renderComposerAttachments,
  };
  for (const mount of registry.composers()) mount(ctx);
  renderComposerAttachments();
}

export function initComposer(callbacks) {
  hooks = { ...hooks, ...callbacks };
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
      restoreWithdrawn(session, (receipt.withdrawn || []).map((item) => item.id));
    } catch (error) {
      alert(error.message);
    }
  });
}

export function saveSubmissions() {
  try {
    const pending = JSON.stringify([...state.submissions]);
    sessionStorage.setItem("zero.submissions", pending);
  } catch {
    // Keep tracking in memory when this tab cannot use session storage.
  }
}

export function restoreWithdrawn(session, ids) {
  const texts = [];
  const attachments = [];
  for (const id of ids) {
    const message = state.submissions.get(id);
    if (message?.session !== session) continue;
    if (message.text) texts.push(message.text);
    if (Array.isArray(message.attachments)) attachments.push(...message.attachments);
    state.submissions.delete(id);
  }
  if (!texts.length && !attachments.length) return;
  if (texts.length) {
    const draft = state.open === session ? $("text").value : readComposerDraft(session);
    writeComposerDraft(session, [...texts, draft].filter(Boolean).join("\n\n"));
  }
  if (attachments.length) {
    hydrateComposerAttachments(session, attachments);
  }
  if (state.open === session) restoreComposerDraft();
  saveSubmissions();
}

export async function checkDeliveries(session) {
  const ids = [...state.submissions].filter(([, m]) => m.session === session)
    .map(([id]) => id);
  const withdrawn = [];
  try {
    for (let i = 0; i < ids.length; i += 100) {
      const batch = ids.slice(i, i + 100);
      const reply = await look("session/deliveries", { session, ids: batch });
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
export function readComposerDraft(session) {
  if (!session) return "";
  if (state.composerDrafts.has(session)) return state.composerDrafts.get(session);
  try { return localStorage.getItem(`zero.draft.${session}`) || ""; }
  catch { return ""; }
}

export function writeComposerDraft(session, text) {
  if (!session) return true;
  state.composerDrafts.set(session, text);
  try {
    const key = `zero.draft.${session}`;
    if (text) localStorage.setItem(key, text);
    else localStorage.removeItem(key);
    return true;
  } catch { return false; }
}

export function saveComposerDraft() {
  const text = $("text").value;
  const saved = writeComposerDraft(state.open, text);
  $("draft-status").textContent = !text || saved
    ? "" : "Draft kept in this tab only";
}

export function restoreComposerDraft() {
  $("text").value = readComposerDraft(state.open);
  saveComposerDraft();
  renderComposerAttachments();
  $("commands").hidden = true;
  sizeComposer();
}

export async function rewindMessage(session, seq) {
  const viewing = state.open;
  const draft = readComposerDraft(session);
  const staged = getComposerAttachments(session);
  const warning = "Return to before this message? Later conversation is left "
    + "behind. Files and memory stay unchanged."
    + (draft || staged.length ? " This replaces your unsent draft." : "");
  if (!confirm(warning)) return false;
  try {
    const result = await change("session/rewind", { session, seq });
    writeComposerDraft(session, result.text || "");
    setComposerAttachments(session, []);
    if (Array.isArray(result.attachments) && result.attachments.length) {
      await hydrateComposerAttachments(session, result.attachments);
    }
    if (state.open === viewing) {
      // Restore before reopening so the previous composer cannot overwrite it.
      if (state.open === session) restoreComposerDraft();
      await hooks.openSession(session);
      $("text").focus();
    }
    return true;
  } catch (error) {
    alert(error.message);
    return false;
  }
}

export function sizeComposer() {
  const input = $("text");
  const style = getComputedStyle(input);
  const padding = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
  const limit = parseFloat(style.lineHeight) * 10 + padding;
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, limit)}px`;
  input.style.overflowY = input.scrollHeight > limit ? "auto" : "hidden";
}

export function browserCommands() {
  const descriptions = {
    "/fork": "fork this session into a new one",
    "/settings": "new-session defaults, model servers and advanced settings",
    "/help": "browser commands; Enter sends, Shift+Enter adds a line",
  };
  return Object.entries(state.hello?.commands || {})
    .filter(([name]) => name !== "/quit")
    .map(([name, about]) => [name, descriptions[name] || about]);
}

export function drawCommands(reset = true) {
  const list = $("commands");
  const text = $("text").value;
  list.replaceChildren();
  if (reset) state.commandIndex = 0;
  const matches = browserCommands().filter(
    ([name]) => text.startsWith("/") && !text.includes(" ")
      && name.startsWith(text),
  );
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

export async function runCommand(text) {
  const [name] = text.split(/\s+/);
  if (name === "/quit") {
    alert("Close the browser tab to leave. ZERO keeps working.");
    return true;
  }
  const customCommand = registry.getCommand(name);
  if (!customCommand && !browserCommands().some(([command]) => command === name)) {
    return false;
  }
  const entered = $("text").value;
  const value = text.slice(name.length).trim();
  const session = state.open;
  try {
    if (customCommand) {
      const handled = await customCommand({ name, value, session, text });
      if (handled === false) return false;
    } else {
    if (state.newSession && !["/new", "/sessions", "/settings", "/help",
      "/rename", "/mode", "/model", "/cd", "/init"].includes(name)) {
      throw new Error("Send the first message to use this session command.");
    }
    switch (name) {
      case "/new": hooks.openNew(); break;
      case "/sessions": showPanel("sessions"); break;
      case "/settings": await hooks.openSettings(); break;
      case "/help":
        alert(browserCommands().map(
          ([command, about]) => `${command}: ${about}`).join("\n"));
        break;
      case "/mode": case "/model": case "/cd":
        if (!value) { showPanel("details"); break; }
        if (name === "/model") await hooks.ensureModelWindow(value);
        if (state.newSession) {
          state.newSession[name === "/cd" ? "workspace" : name.slice(1)] = value;
          $("details").draftRow = null;
          hooks.drawDetails();
          break;
        }
        await change("session/set", {
          session, [name === "/cd" ? "workspace" : name.slice(1)]: value,
        });
        break;
      case "/rename":
        if (!value) throw new Error("Use /rename followed by a name");
        if (state.newSession) { state.newSession.title = value; hooks.drawHead(); }
        else await change("session/rename", { session, title: value });
        break;
      case "/archive":
        await change(rowOf(session).archived
          ? "session/unarchive" : "session/archive", { session });
        break;
      case "/delete":
        if (confirm("Move this session to the trash?")) {
          await change("session/delete", { session });
        }
        break;
      case "/fork": {
        const reply = await change("session/fork", { session, source: "web" });
        if (Array.isArray(reply.attachments) && reply.attachments.length) {
          await hydrateComposerAttachments(reply.session.id, reply.attachments);
        }
        await hooks.openSession(reply.session.id);
        break;
      }
      case "/continue":
        await change("session/continue", { session, source: "web" });
        break;
      case "/copy": {
        const last = lastAssistantText(state.records);
        if (last) await navigator.clipboard.writeText(last);
        break;
      }
      case "/rewind": await historyActions(session); return true;
      case "/init": {
        const reply = await change("session/submit", {
          ...(session ? { session } : {}),
          source: "web", text: state.hello.init_request,
        });
        if (!session) await hooks.openSession(reply.session);
        break;
      }
      default: throw new Error("Unknown command");
    }
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

export async function historyActions(session) {
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

export async function send(event) {
  if (event) event.preventDefault();
  const original = $("text").value;
  const button = $("send");
  let text = original.trim();
  const draft = state.newSession;
  const session = state.open;
  const staged = getComposerAttachments(session);
  if ((!text && !staged.length) || button.disabled) return;
  const view = state.openVersion;
  if (!session && !draft) return;
  button.disabled = true;
  try {
    if (text) {
      if (draft && text === "/init") text = state.hello.init_request;
      else if (await runCommand(text)) return;
    }
    await state.sessionRead;
    while (state.openVersion === view && state.settingUpdates.size) {
      await Promise.all(state.settingUpdates);
    }
    if (state.openVersion !== view) return;
    const runs = state.runsWith;
    if (!runs) throw new Error("Session settings are still loading.");
    const { model, thinking_effort: effort } = runs;
    const row = rowOf(session);
    if (draft || !row || (!row.ask && !["running", "waiting"].includes(row.state))) {
      const support = await modelCapabilities(model || row?.model, true, true);
      if (state.openVersion !== view) return;
      if (state.settingUpdates.size || state.runsWith?.model !== model
          || state.runsWith?.thinking_effort !== effort) {
        throw new Error("Session settings changed. Your message is in the composer.");
      }
      const problem = effortProblem(support, effort || "model_default");
      if (problem) {
        await hooks.drawDetails();
        alert(problem);
        return;
      }
    }
    const attachmentIds = staged.map((item) => item.attachment_id);
    if (draft) {
      const fields = { ...draft, text, source: "web" };
      for (const [cap] of UTILITY_CAPABILITIES) {
        if (!fields[cap]) delete fields[cap];
      }
      if (attachmentIds.length) fields.attachments = attachmentIds;
      const content = JSON.stringify(fields);
      if (state.firstSend?.content !== content) {
        state.firstSend = { content, id: newId() };
      }
      const reply = attachmentIds.length
        ? await change("session/send_with_attachments", {
          ...fields, id: state.firstSend.id,
        })
        : await change("session/send", {
          ...fields, id: state.firstSend.id,
        });
      if (attachmentIds.length) setComposerAttachments(null, []);
      if (state.newSession === draft) {
        const remaining = $("text").value;
        if (remaining !== original) writeComposerDraft(reply.session, remaining);
        await hooks.openSession(reply.session);
      }
      return;
    }
    const id = newId();
    const queued = Boolean(rowOf(session)?.ask) || $("queue").checked;
    const payload = {
      id, session, text, source: "web", when_busy: queued ? "queue" : "steer",
    };
    if (attachmentIds.length) payload.attachments = attachmentIds;
    const receipt = attachmentIds.length
      ? await change("session/submit_with_attachments", payload)
      : await change("session/submit", payload);
    if (readComposerDraft(session) === original) {
      writeComposerDraft(session, "");
    }
    if (attachmentIds.length) setComposerAttachments(session, []);
    if (state.open === session && $("text").value === original) {
      $("text").value = "";
      saveComposerDraft();
      sizeComposer();
      $("queue").checked = false;
    }
    if (["steered", "queued"].includes(receipt.handled_as)) {
      state.submissions.set(id, {
        session, text: original, attachments: staged,
      });
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
