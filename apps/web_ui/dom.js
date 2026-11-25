/* Shared DOM utilities, formatting helpers, theme state, and shared page state. */

export const $ = (id) => document.getElementById(id);

export const GAP_SECONDS = 10 * 60;
export const HIDDEN_SECONDS = 60;
export const PAGE_RECORDS = 150;
export const RESULT_CHARS = 20000;
export const WIDTHS = { left: [245, 200, 460], right: [285, 260, 640] };

export const state = {
  hello: null,
  sessions: [],
  sessionMap: new Map(),
  open: null,
  newSession: null,
  records: [],
  earlier: 0,
  runsWith: null,
  context: null,
  events: null,
  asleep: false,
  calls: new Map(),
  lastSeq: 0,
  lastTime: null,
  draft: null,
  hiddenTimer: null,
  seenSent: 0,
  turn: null,
  archived: false,
  stoppingJobs: new Set(),
  detailsVersion: 0,
  settings: [],
  commandIndex: 0,
  launch: {},
  expandedJobs: new Map(),
  expandedFamilies: new Map(),
  settingsDraft: new Map(),
  settingsErrors: new Map(),
  settingsSaving: false,
  settingsView: "General",
  modelLookup: null,
  modelServer: null,
  modelDirectories: new Map(),
  modelVersions: new Map(),
  modelPanelVersion: 0,
  rewindPoints: new Set(),
  composerDrafts: new Map(),
  submissions: new Map(),
  jobs: new Map(),
  settingUpdates: new Set(),
  settingsStatus: "",
  sessionRead: null,
  features: new Map(),
  utilities: new Map(),
  composerAttachments: new Map(),
  turnStarted: null,
  openVersion: 0,
  firstSend: null,
};

export const UTILITY_CAPABILITIES = [
  ["search_service", "Search"],
  ["stt_service", "Speech to text"],
  ["tts_service", "Text to speech"],
  ["image_service", "Images"],
  ["video_service", "Video"],
];

export function draftKey(session = state.open) {
  return session || "__new__";
}

export function formatBytes(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KB`;
  if (n < 1024 * 1024 * 1024) {
    return `${(n / (1024 * 1024)).toFixed(n < 10 * 1024 * 1024 ? 1 : 0)} MB`;
  }
  return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

try {
  state.submissions = new Map(JSON.parse(
    sessionStorage.getItem("zero.submissions") || "[]",
  ));
} catch {
  // This tab can still track submissions in memory.
}

export function rowOf(id) {
  if (!id) return null;
  return state.sessionMap.get(id) || null;
}

export function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

export function folderName(path) {
  if (!path || path === "/") return "/";
  const parts = path.replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] || path;
}

export function seconds(value) {
  if (value === null || value === undefined) return "";
  const s = Number(value);
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}

export function clock(iso) {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "";
  return when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function tokens(count) {
  const n = Number(count) || 0;
  return n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
}

export function stateWord(row) {
  if (row.ask || row.state === "waiting") return "waiting";
  if (row.state === "running") return "running";
  if (row.state === "failed") return "failed";
  return row.unseen ? "news" : "idle";
}

export function sourceWho(source) {
  if (!source || source === "web" || String(source).startsWith("terminal")) {
    return "You";
  }
  if (source === "mcp") return "Agent";
  if (source === "zero" || source === "subagent") return "ZERO";
  return String(source);
}

export function effortLabel(value) {
  return value === "model_default" ? "Model default" : value;
}

export function effortProblem(reply, value) {
  if (reply?.tools_error) return reply.tools_error;
  if (reply?.thinking_efforts?.[value] === false) {
    return `${effortLabel(value)} is unavailable on this route. `
      + "Choose a supported thinking effort in Session settings.";
  }
  return "";
}

export function showPanel(name) {
  const frame = $("frame");
  frame.classList.remove("show-sessions", "show-session", "show-details");
  frame.classList.add(`show-${name}`);
  if (name === "details") frame.classList.remove("no-details");
}

export function actionDialog(title) {
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

export function failureDetails(lines) {
  const details = el("details", "model-failure-details");
  details.append(el("summary", null, "Details"));
  for (const line of lines) details.append(el("div", null, line));
  return details;
}

export function modelFailureView(failure) {
  const node = el("div", "model-failure");
  const title = failure.server_title || failure.server;
  node.append(el("strong", null, `Model request failed · ${title}`));
  if (failure.next_action === "retry") {
    node.append(el("div", "sub", "Retrying model request…"));
  } else if (failure.next_action === "fallback") {
    node.append(el("div", "sub", `Using fallback · ${failure.fallback}`));
  }
  const operations = {
    generate_reply: "Generate reply",
    test_model: "Test model",
    compact_context: "Compact context",
  };
  const lines = [
    operations[failure.operation] || failure.operation,
    failure.http_status == null ? null : `HTTP ${failure.http_status}`,
    failure.provider_code,
    failure.provider_message,
    failure.observation,
  ];
  node.append(failureDetails(lines.filter(Boolean)));
  return node;
}

let themePreference = "system";
try {
  themePreference = localStorage.getItem("zero.theme") || "system";
} catch {
  // System appearance works without browser storage.
}
const darkScheme = typeof matchMedia === "function"
  ? matchMedia("(prefers-color-scheme: dark)") : null;
const reducedMotion = typeof matchMedia === "function"
  ? matchMedia("(prefers-reduced-motion: reduce)") : null;

export function getThemePreference() {
  return themePreference;
}

export function setThemePreference(value) {
  themePreference = value;
  try {
    localStorage.setItem("zero.theme", themePreference);
  } catch {
    // This tab still uses the chosen theme if storage is unavailable.
  }
  applyTheme();
}

export function applyTheme() {
  if (!darkScheme || !reducedMotion) return;
  const dark = themePreference === "dark"
    || (themePreference === "system" && darkScheme.matches);
  const theme = dark ? "dark" : "light";
  document.documentElement.dataset.theme = theme;
  const still = reducedMotion.matches ? "-still" : "";
  document.querySelectorAll(".brand source").forEach((source) => {
    source.srcset = `/ui/wordmark-${theme}${still}.svg`;
  });
  const img = document.querySelector(".brand img");
  if (img) img.src = `/ui/wordmark-${theme}${still}.svg`;
  document.querySelectorAll('link[rel="icon"]').forEach((icon) => {
    icon.removeAttribute("media");
    icon.href = `/ui/favicon-${theme}${still}.svg`;
  });
}

if (darkScheme && reducedMotion) {
  darkScheme.addEventListener("change", applyTheme);
  reducedMotion.addEventListener("change", applyTheme);
}
