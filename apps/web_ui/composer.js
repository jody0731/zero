/* Composer, unsent drafts, submission tracking, slash commands, and rewind. */

import { change, look, modelCapabilities, newId } from "./api.js";
import { registry } from "./components.js";
import {
  $, actionDialog, effortProblem, el, rowOf, showPanel, state,
} from "./dom.js";
import { lastAssistantText } from "./timeline.js";

let hooks = {
  openSession: () => {},
  openNew: () => {},
  openSettings: () => {},
  drawDetails: () => {},
  drawHead: () => {},
  ensureModelWindow: () => {},
};

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
  $("commands").hidden = true;
  sizeComposer();
}

export async function rewindMessage(session, seq) {
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
  if (!text || button.disabled) return;
  const draft = state.newSession;
  const session = state.open;
  const view = state.openVersion;
  if (!session && !draft) return;
  button.disabled = true;
  try {
    if (draft && text === "/init") text = state.hello.init_request;
    else if (await runCommand(text)) return;
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
        await hooks.openSession(reply.session);
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
