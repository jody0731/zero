/* Settings dialog: General, component tabs, and Advanced tabs. */

import { change, look } from "./api.js";
import { registry } from "./components.js";
import {
  $, el, getThemePreference, setThemePreference, state,
} from "./dom.js";
import { drawDetails, ensureModelWindow, fieldInput } from "./details.js";

export function initSettings() {
  $("settings-open").addEventListener("click", () => {
    openSettings().catch((error) => alert(error.message));
  });
  $("settings-close").addEventListener("click", leaveSettings);
  $("settings-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    leaveSettings();
  });
}

export async function openSettings(view = "General") {
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
  const tabNames = ["General", ...registry.settingsTabs(), "Advanced"];
  for (const name of [...new Set(tabNames)]) {
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
  const customTab = registry.getSettingsTab(view);
  if (customTab) return customTab(body);
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
    select.value = getThemePreference();
    select.addEventListener("change", () => setThemePreference(select.value));
    label.append(select);
    body.append(label, el("h3", "settings-section-title", "Defaults"),
      el("p", "sub", "For new sessions. Applied when you leave settings."));
  }
  const groups = new Map();
  for (const setting of state.settings.filter((each) => each.shown
    && (view === "General" ? each.group === "Default" : each.group !== "Default"))) {
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

export async function leaveSettings() {
  if (state.settingsSaving) return;
  state.settingsSaving = true;
  const back = $("settings-close");
  back.disabled = true;
  back.textContent = "Updating…";
  const controls = [...$("settings-dialog").querySelectorAll(
    "input, select, button",
  )].filter((control) => !control.disabled);
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
        if (setting.kind === "model") await ensureModelWindow(value);
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
