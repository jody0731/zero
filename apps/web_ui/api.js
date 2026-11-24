/* HTTP door requests, browser pairing, and model capability lookups. */

import { effortLabel, effortProblem, state } from "./dom.js";

let wakeHandler = () => {};

export function setWakeHandler(fn) {
  wakeHandler = fn;
}

export function newId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return String(Date.now()) + Math.random().toString(16).slice(2);
}

export async function pairBrowser() {
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
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key }),
  });
  if (!response.ok) {
    throw new Error("This browser is not paired. Run zero web once.");
  }
}

export async function post(payload, restored = false) {
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
export async function look(message, fields) {
  const reply = await post({ message, ...(fields || {}) });
  if (!reply.ok) throw new Error(reply.error);
  return reply;
}

// A change: its own id, sent again with the same id if the first try
// never came back, so the server runs it once
export async function change(message, fields) {
  const payload = { message, id: newId(), ...(fields || {}) };
  let reply;
  try {
    reply = await post(payload);
  } catch (error) {
    if (error.status === 401) throw error;
    reply = await post(payload);
  }
  wakeHandler();
  if (!reply.ok) {
    const error = new Error(reply.error);
    error.modelFailure = reply.model_failure;
    throw error;
  }
  return reply;
}

export function clearModelFeatures(model = null) {
  if (!model) {
    state.features.clear();
    return;
  }
  for (const key of [...state.features.keys()]) {
    try {
      if (JSON.parse(key)[0] === model) state.features.delete(key);
    } catch {
      // Ignore unexpected keys.
    }
  }
}

export async function modelCapabilities(model, tools, fresh = false) {
  if (!model) return null;
  const key = JSON.stringify([model, tools]);
  const saved = state.features.get(key);
  if (!fresh && saved && Date.now() < saved.until) {
    return saved.reply.catch(() => null);
  }
  const reply = change("model/features", { model, cached_only: true, tools });
  state.features.set(key, { until: Date.now() + 15000, reply });
  return fresh ? reply : reply.catch(() => null);
}

export function labelEfforts(input, reply, note) {
  for (const option of input.options) {
    const supported = reply?.thinking_efforts?.[option.value];
    option.disabled = supported === false;
    const suffix = supported === false ? " (unavailable)"
      : supported === null ? " (unverified)" : "";
    option.textContent = effortLabel(option.value) + suffix;
  }
  note.textContent = effortProblem(reply, input.value);
}
