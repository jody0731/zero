/* Component web UI host registry and dynamic module/stylesheet loader. */

export const SLOTS = [
  "ask",
  "entry",
  "toolAction",
  "details",
  "settings",
  "sessionBadge",
  "command",
];

export function createRegistry() {
  const asks = new Map();
  const entries = new Map();
  const toolActions = [];
  const detailsSections = [];
  const settingsTabs = new Map();
  const sessionBadges = [];
  const commands = new Map();

  return {
    registerAsk(kind, render) {
      asks.set(kind, render);
    },
    registerEntry(type, render) {
      entries.set(type, render);
    },
    registerToolAction(fn) {
      toolActions.push(fn);
    },
    registerDetails(fn) {
      detailsSections.push(fn);
    },
    registerSettings(tabName, render) {
      settingsTabs.set(tabName, render);
    },
    registerSessionBadge(fn) {
      sessionBadges.push(fn);
    },
    registerCommand(name, handler) {
      commands.set(name, handler);
    },
    getAsk(kind) {
      return asks.get(kind) || null;
    },
    getEntry(type) {
      return entries.get(type) || null;
    },
    toolActions() {
      return [...toolActions];
    },
    detailsSections() {
      return [...detailsSections];
    },
    settingsTabs() {
      return [...settingsTabs.keys()];
    },
    getSettingsTab(name) {
      return settingsTabs.get(name) || null;
    },
    sessionBadges() {
      return [...sessionBadges];
    },
    getCommand(name) {
      return commands.get(name) || null;
    },
    reset() {
      asks.clear();
      entries.clear();
      toolActions.length = 0;
      detailsSections.length = 0;
      settingsTabs.clear();
      sessionBadges.length = 0;
      commands.clear();
    },
  };
}

export const registry = createRegistry();

export function createHost(base = {}, targetRegistry = registry) {
  return {
    ...base,
    registry: targetRegistry,
    ask: (kind, render) => targetRegistry.registerAsk(kind, render),
    entry: (type, render) => targetRegistry.registerEntry(type, render),
    toolAction: (fn) => targetRegistry.registerToolAction(fn),
    details: (fn) => targetRegistry.registerDetails(fn),
    settings: (tabName, render) => targetRegistry.registerSettings(tabName, render),
    sessionBadge: (fn) => targetRegistry.registerSessionBadge(fn),
    command: (name, handler) => targetRegistry.registerCommand(name, handler),
  };
}

function defaultAttachStyle(href) {
  if (typeof document === "undefined" || !document.head) return;
  const existing = document.head.querySelector(`link[rel="stylesheet"][href="${href}"]`);
  if (existing) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = href;
  document.head.append(link);
}

export async function loadComponents(host, opts = {}) {
  const fetchFn = opts.fetch || globalThis.fetch;
  const importModule = opts.importModule || ((url) => import(url));
  const attachStyle = opts.attachStyle || defaultAttachStyle;
  const manifestUrl = opts.url || "/ui/components.json";
  const loaded = [];
  const failed = [];
  if (!fetchFn) return { loaded, failed };
  let items = [];
  try {
    const response = await fetchFn(manifestUrl, {
      headers: { Accept: "application/json" },
    });
    if (!response || !response.ok) return { loaded, failed };
    const data = await response.json();
    items = Array.isArray(data) ? data : [];
  } catch (error) {
    failed.push({ name: "manifest", error: String(error?.message || error) });
    return { loaded, failed };
  }
  for (const item of items) {
    const name = item?.name || "unknown";
    try {
      if (item.style) attachStyle(item.style);
      if (item.module) {
        const mod = await importModule(item.module);
        if (typeof mod?.register === "function") {
          await mod.register(host);
        }
      }
      loaded.push(name);
    } catch (error) {
      failed.push({ name, error: String(error?.message || error) });
    }
  }
  return { loaded, failed };
}
