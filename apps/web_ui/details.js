/* Right-hand details panel: session settings, family tree, context, and jobs. */

import { change, labelEfforts, look, modelCapabilities } from "./api.js";
import { registry } from "./components.js";
import {
  $, actionDialog, effortLabel, el, folderName, modelFailureView, rowOf,
  showPanel, state, tokens,
} from "./dom.js";
import { sessionBranch } from "./sessions.js";

let hooks = {
  openNew: () => {},
};

export function initDetails(callbacks) {
  hooks = { ...hooks, ...callbacks };
}

export function modelChoices(current) {
  const models = [...state.modelDirectories.values()].flatMap((row) => row.models);
  return [...new Set([current, ...models.sort()].filter(Boolean))];
}

export function updateModelPickers() {
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

export function nextModelVersion(name) {
  const version = (state.modelVersions.get(name) || 0) + 1;
  state.modelVersions.set(name, version);
  return version;
}

export function acceptModelDirectory(name, version, row) {
  if (state.modelVersions.get(name) !== version) return false;
  nextModelVersion(name);
  if (row) state.modelDirectories.set(name, row);
  else state.modelDirectories.delete(name);
  updateModelPickers();
  return true;
}

export async function refreshModels() {
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
  } catch {
    // Keep the last directory if the local server couldn't be reached.
  } finally {
    if (state.modelLookup === pending) state.modelLookup = null;
  }
}

export function fieldInput(setting, value) {
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
      const option = el("option", null, effortLabel(choice));
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

export function modelButton(text, pressed) {
  const button = el("button", null, text);
  button.type = "button";
  button.addEventListener("click", pressed);
  return button;
}

export function modelInput(label, type = "text", required = true) {
  const field = el("label", null, label);
  const input = el("input");
  input.type = type;
  input.required = required;
  if (type === "password") input.autocomplete = "off";
  field.append(input);
  return [field, input];
}

export function modelForm(fields, label, submit, close, notice) {
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

export async function ensureModelWindow(model) {
  if (!model) return;
  const found = await look("model/window", { model });
  if (found.context_window !== null) return;
  await new Promise((resolve, reject) => {
    const dialog = actionDialog("Context window");
    const [field, input] = modelInput("Context window in tokens", "number");
    input.min = "1";
    dialog.append(el("p", null, "Enter the window configured for " + model + "."));
    dialog.append(modelForm([field], "Save", async () => {
      await change("model/window/set", {
        model, url: found.url, context_window: Number(input.value),
      });
      resolve();
      dialog.close();
    }, () => dialog.close()));
    dialog.addEventListener("close", () => {
      reject(new Error("The model needs its context window before it can be used."));
    });
    input.focus();
  });
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
        const sub = "/" + parts.slice(0, index + 1).join("/");
        const crumb = el("button", "ghost", part);
        crumb.title = sub;
        crumb.disabled = index === parts.length - 1;
        crumb.addEventListener("click", () => show(sub));
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
  const status = el("div", "sub save-status", state.settingsStatus);
  const thinkingNote = el("div", "sub");
  thinkingNote.setAttribute("aria-live", "polite");
  status.setAttribute("aria-live", "polite");
  const sayStatus = (message) => {
    if (row.id === state.open || row === state.newSession) {
      state.settingsStatus = message;
    }
    status.textContent = message;
  };
  const update = async (name, value, control) => {
    const previous = name === "thinking_effort" ? runs[name] : row[name];
    control.disabled = true;
    sayStatus("Updating…");
    try {
      if (name === "model") await ensureModelWindow(value);
      if (row === state.newSession) {
        row[name] = value;
        sayStatus("Updated");
        return;
      }
      const reply = await change("session/set", { session: row.id, [name]: value });
      if (state.open === row.id) {
        state.runsWith = { ...state.runsWith, ...reply.set };
        Object.assign(rowOf(row.id) || row, reply.set);
      }
      Object.assign(runs, reply.set);
      sayStatus(reply.next_turn ? "Applies next turn" : "Updated");
    } catch (error) {
      if (control.tagName === "SELECT") control.value = previous || "";
      sayStatus(error.message);
      throw error;
    } finally {
      control.disabled = false;
    }
  };
  const updates = state.settingUpdates;
  const apply = (name, value, control) => {
    const pending = update(name, value, control);
    updates.add(pending);
    const done = () => updates.delete(pending);
    pending.then(done, done);
    return pending;
  };
  for (const [name, label, value] of [
    ["mode", "Mode", row.mode],
    ["model", "Model", row.model],
    ["thinking_effort", "Thinking", runs.thinking_effort],
  ]) {
    const setting = state.settings.find((each) => each.name === name)
      || {
        kind: name === "thinking_effort" ? "choice" : name,
        choices: value ? [value] : [],
        label,
      };
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
  }), thinkingNote, status);
  const capabilities = async () => {
    const model = inputs.model.value.trim();
    const reply = await modelCapabilities(model, true);
    if (model === inputs.model.value.trim()) {
      labelEfforts(inputs.thinking_effort, reply, thinkingNote);
    }
  };
  inputs.model.addEventListener("change", capabilities);
  inputs.thinking_effort.addEventListener("change", capabilities);
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

export function drawUsage() {
  $("usage").hidden = Boolean(state.newSession);
  const context = state.context || {};
  const cache = context.cached_share;
  $("usage").textContent = `Context ${tokens(context.tokens)}`
    + (context.line ? ` / ${tokens(context.line)}` : "")
    + ` · Cache ${cache === null || cache === undefined
      ? "—" : `${Math.round(cache * 100)}%`}`;
  $("usage").title = "Cached input tokens in the last model call";
}

export async function drawDetails() {
  const panel = $("details");
  const draft = state.newSession;
  const row = draft || rowOf(state.open);
  drawUsage();
  const version = ++state.detailsVersion;
  if (draft && panel.draftRow === draft) return;
  panel.draftRow = draft;
  const editing = state.settingUpdates.size || panel.contains(document.activeElement);
  if (!draft && row && editing) {
    const family = familySection(row);
    const oldFamily = panel.querySelector(".session-family");
    if (oldFamily) {
      if (family) oldFamily.replaceWith(family);
      else oldFamily.remove();
    } else if (family) panel.append(family);
    for (const render of registry.detailsSections()) {
      const section = await render(row);
      if (section && version === state.detailsVersion && state.open === row.id) {
        const old = section.id ? panel.querySelector(`#${section.id}`) : null;
        if (old) old.replaceWith(section);
      }
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
    button.addEventListener("click", () => hooks.openNew());
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
  for (const render of registry.detailsSections()) {
    const section = await render(row);
    if (section && version === state.detailsVersion && state.open === row.id) {
      panel.append(section);
    }
  }
}
