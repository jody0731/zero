/* Session list state (snapshot + incremental diffs) and in-place DOM reconciliation. */

import { change } from "./api.js";
import { registry } from "./components.js";
import { $, el, folderName, rowOf, state, stateWord } from "./dom.js";

const branchNodes = new Map();
let hooks = {
  openSession: () => {},
  drawHead: () => {},
  historyActions: () => {},
};

export function initSessions(callbacks) {
  hooks = { ...hooks, ...callbacks };
}

function sessionOrder(a, b) {
  const waitDiff = (b.state === "waiting" ? 1 : 0) - (a.state === "waiting" ? 1 : 0);
  if (waitDiff) return waitDiff;
  const updDiff = (b.updated_at || 0) - (a.updated_at || 0);
  if (updDiff) return updDiff;
  return (b.created_at || 0) - (a.created_at || 0);
}

export function applyListEvent(data) {
  if (!data || typeof data !== "object") return false;
  if (Array.isArray(data.sessions)) {
    state.sessionMap.clear();
    for (const row of data.sessions) state.sessionMap.set(row.id, row);
  } else if (Array.isArray(data.upsert) || Array.isArray(data.remove)) {
    for (const id of data.remove || []) state.sessionMap.delete(id);
    for (const row of data.upsert || []) state.sessionMap.set(row.id, row);
  } else {
    return false;
  }
  state.sessions = [...state.sessionMap.values()].sort(sessionOrder);
  return true;
}

function badgesText(row, kidCount) {
  const badges = [];
  for (const badge of registry.sessionBadges()) {
    const label = badge(row, kidCount);
    if (label) badges.push(label);
  }
  if (kidCount) badges.push(`+${kidCount}`);
  return [
    folderName(row.workspace),
    row.started_by === "zero" ? "subagent" : (row.parent ? "fork" : ""),
    ...badges,
  ].filter(Boolean).join(" · ");
}

function reconcileChildren(parent, ordered) {
  const keep = new Set(ordered);
  for (const child of [...parent.children]) {
    if (!keep.has(child)) child.remove();
  }
  ordered.forEach((node, index) => {
    const current = parent.children[index];
    if (current !== node) parent.insertBefore(node, current || null);
  });
}

function buildToggle(id, titleText) {
  const toggle = el("button", "tree-toggle");
  toggle.type = "button";
  toggle.dataset.treeToggle = id;
  const update = (control, expanded, label) => {
    control.textContent = expanded ? "▾" : "▸";
    control.setAttribute("aria-expanded", String(expanded));
    control.setAttribute("aria-label",
      `${expanded ? "Collapse" : "Expand"} ${label}`);
  };
  const expanded = state.expandedFamilies.get(id) ?? true;
  update(toggle, expanded, titleText || id);
  toggle.addEventListener("click", () => {
    const next = !(state.expandedFamilies.get(id) ?? true);
    state.expandedFamilies.set(id, next);
    const label = rowOf(id)?.title || id;
    document.querySelectorAll("[data-tree-parent]").forEach((list) => {
      if (list.dataset.treeParent === id) list.hidden = !next;
    });
    document.querySelectorAll("[data-tree-toggle]").forEach((control) => {
      if (control.dataset.treeToggle === id) update(control, next, label);
    });
  });
  return toggle;
}

function syncToggle(toggle, id, titleText) {
  const expanded = state.expandedFamilies.get(id) ?? true;
  const glyph = expanded ? "▾" : "▸";
  if (toggle.textContent !== glyph) toggle.textContent = glyph;
  const expStr = String(expanded);
  if (toggle.getAttribute("aria-expanded") !== expStr) {
    toggle.setAttribute("aria-expanded", expStr);
  }
  const label = `${expanded ? "Collapse" : "Expand"} ${titleText || id}`;
  if (toggle.getAttribute("aria-label") !== label) {
    toggle.setAttribute("aria-label", label);
  }
}

function sessionActions(initialRow) {
  const id = initialRow.id;
  const menu = el("details", "session-actions");
  const summary = el("summary", null, "⋯");
  summary.setAttribute("aria-label", `Actions for ${initialRow.title || id}`);
  menu.append(summary);
  const choices = el("div", "session-menu");
  choices.setAttribute("popover", "auto");
  menu.append(choices);

  const currentRow = () => rowOf(id) || initialRow;

  menu.addEventListener("toggle", () => {
    if (!menu.open) { choices.hidePopover(); return; }
    const bounds = summary.getBoundingClientRect();
    choices.showPopover();
    choices.style.left = `${Math.max(8, Math.min(bounds.right - 160,
      window.innerWidth - 176))}px`;
    choices.style.top = `${Math.max(8, Math.min(bounds.bottom + 4,
      window.innerHeight - choices.offsetHeight - 8))}px`;
  });
  choices.addEventListener("toggle", () => {
    if (!choices.matches(":popover-open")) {
      menu.open = false;
      if (choices.querySelector(".rename")) populateButtons();
    }
  });

  let archiveButton = null;

  function populateButtons() {
    choices.replaceChildren();
    const row = currentRow();
    const actions = [
      ["Rename", async () => {
        const latest = currentRow();
        const input = el("input", "rename");
        input.value = latest.title || "";
        input.setAttribute("aria-label", "Session name");
        choices.replaceChildren(input);
        input.focus();
        input.select();
        input.addEventListener("keydown", async (event) => {
          if (event.key === "Escape") {
            menu.open = false;
            input.blur();
            populateButtons();
            drawSessions();
          }
          if (event.key === "Enter" && input.value.trim()) {
            input.disabled = true;
            try {
              const title = input.value.trim();
              await change("session/rename", { session: id, title });
              const current = rowOf(id);
              if (current) current.title = title;
              hooks.drawHead();
              menu.open = false;
              input.blur();
              populateButtons();
              drawSessions();
            } catch (error) {
              input.disabled = false;
              input.focus();
              alert(error.message);
            }
          }
        });
      }],
      ["History…", () => hooks.historyActions(id)],
      ["Fork", async () => {
        const reply = await change("session/fork", {
          session: id, source: "web",
        });
        await hooks.openSession(reply.session.id);
      }],
      [row.archived ? "Restore" : "Archive", () => {
        const latest = currentRow();
        return change(
          latest.archived ? "session/unarchive" : "session/archive",
          { session: id },
        );
      }],
      ["Copy link", () => navigator.clipboard.writeText(
        `${location.origin}/#session=${encodeURIComponent(id)}`)],
      ["Delete", async () => {
        if (confirm("Move this session to the trash?")) {
          await change("session/delete", { session: id });
        }
      }],
    ];
    archiveButton = null;
    for (const [name, action] of actions) {
      const button = el("button", null, name);
      button.type = "button";
      if (name === "Archive" || name === "Restore") archiveButton = button;
      button.addEventListener("click", () => {
        if (name !== "Rename") {
          menu.open = false;
          button.blur();
        }
        Promise.resolve(action()).catch((error) => alert(error.message));
      });
      choices.append(button);
    }
  }

  populateButtons();
  menu._syncRow = (row) => {
    const label = `Actions for ${row.title || id}`;
    if (summary.getAttribute("aria-label") !== label) {
      summary.setAttribute("aria-label", label);
    }
    if (archiveButton && !choices.querySelector(".rename")) {
      const next = row.archived ? "Restore" : "Archive";
      if (archiveButton.textContent !== next) archiveButton.textContent = next;
    }
  };
  return menu;
}

export function sessionRow(row, kidCount, family = false) {
  const item = el("div", "row" + (family ? " family-row tree-session" : "")
    + (row.id === state.open ? " open" : ""));
  item.tabIndex = 0;
  item.title = `${row.title || "Untitled"} · ${row.workspace || ""}`;
  if (family) item.dataset.familySession = row.id;
  else item.dataset.session = row.id;
  if (row.id === state.open) item.setAttribute("aria-current", "true");
  const dot = el("span", "dot" + (row.unseen ? " unseen" : ""));
  const title = el("span", "title tree-title", row.title || "(untitled)");
  const word = stateWord(row);
  const status = el("span", `state ${word}`,
    ["running", "waiting", "failed"].includes(word) ? word : "");
  const where = el("span", "where", badgesText(row, kidCount));
  where.title = row.workspace || "";
  item.append(dot, title, status, where);
  const actions = family ? null : sessionActions(row);
  if (actions) item.append(actions);
  item.addEventListener("click", (event) => {
    if (!event.target.closest("button, input, details")) hooks.openSession(row.id);
  });
  item.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target === item) hooks.openSession(row.id);
  });
  return { item, dot, title, status, where, actions };
}

function updateRowParts(entry, row, kidCount) {
  const { item, dot, title, status, where, actions } = entry;
  const isOpen = row.id === state.open;
  item.classList.toggle("open", isOpen);
  if (isOpen) item.setAttribute("aria-current", "true");
  else item.removeAttribute("aria-current");
  const nextTitleAttr = `${row.title || "Untitled"} · ${row.workspace || ""}`;
  if (item.title !== nextTitleAttr) item.title = nextTitleAttr;
  dot.classList.toggle("unseen", Boolean(row.unseen));
  const titleText = row.title || "(untitled)";
  if (title.textContent !== titleText) title.textContent = titleText;
  const word = stateWord(row);
  const statusClass = `state ${word}`;
  if (status.className !== statusClass) status.className = statusClass;
  const statusText = ["running", "waiting", "failed"].includes(word) ? word : "";
  if (status.textContent !== statusText) status.textContent = statusText;
  const whereText = badgesText(row, kidCount);
  if (where.textContent !== whereText) where.textContent = whereText;
  const workspace = row.workspace || "";
  if (where.title !== workspace) where.title = workspace;
  if (actions && actions._syncRow) actions._syncRow(row);
}

function reconcileBranch(row, children, seen) {
  if (seen.has(row.id)) return null;
  seen.add(row.id);
  const kids = children.get(row.id) || [];
  let entry = branchNodes.get(row.id);
  if (!entry) {
    const branch = el("li", "session-branch");
    branch.dataset.branchSession = row.id;
    const parts = sessionRow(row, kids.length, false);
    branch.append(parts.item);
    entry = { branch, ...parts, toggle: null, nested: null };
    branchNodes.set(row.id, entry);
  } else {
    updateRowParts(entry, row, kids.length);
  }

  if (kids.length) {
    if (!entry.toggle) {
      entry.toggle = buildToggle(row.id, row.title);
      entry.item.firstElementChild.replaceWith(entry.toggle);
    } else {
      syncToggle(entry.toggle, row.id, row.title);
    }
    if (!entry.nested) {
      entry.nested = el("ul", "session-children");
      entry.nested.dataset.treeParent = row.id;
      entry.branch.append(entry.nested);
    }
    const expanded = state.expandedFamilies.get(row.id) ?? true;
    entry.nested.hidden = !expanded;
    const childNodes = [];
    for (const kid of kids) {
      const node = reconcileBranch(kid, children, seen);
      if (node) childNodes.push(node);
    }
    reconcileChildren(entry.nested, childNodes);
  } else {
    if (entry.toggle) {
      entry.toggle.replaceWith(entry.dot);
      entry.toggle = null;
    }
    if (entry.nested) {
      entry.nested.remove();
      entry.nested = null;
    }
  }
  return entry.branch;
}

export function drawSessions() {
  const list = $("session-list");
  const rows = state.sessions
    .filter((row) => Boolean(row.archived) === state.archived)
    .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
  const ids = new Set(rows.map((row) => row.id));
  const children = new Map();
  for (const row of rows) {
    if (row.parent && ids.has(row.parent)) {
      if (!children.has(row.parent)) children.set(row.parent, []);
      children.get(row.parent).push(row);
    }
  }
  // The ones that wait for you first, then the rest by their last change
  const tops = rows.filter((row) => !row.parent || !ids.has(row.parent));
  tops.sort((a, b) => (stateWord(b) === "waiting") - (stateWord(a) === "waiting"));

  const seen = new Set();
  const topNodes = [];
  for (const row of tops) {
    const branch = reconcileBranch(row, children, seen);
    if (branch) topNodes.push(branch);
  }
  for (const [id, entry] of [...branchNodes.entries()]) {
    if (!seen.has(id)) {
      entry.branch.remove();
      branchNodes.delete(id);
    }
  }
  if (!topNodes.length) {
    const existingEmpty = list.children.length === 1
      && list.firstElementChild.classList.contains("loading");
    if (!existingEmpty) list.replaceChildren(el("li", "loading", "—"));
    return;
  }
  reconcileChildren(list, topNodes);
}

export function sessionBranch(row, children, seen, family = true) {
  if (seen.has(row.id)) return null;
  seen.add(row.id);
  const kids = children.get(row.id) || [];
  const branch = el("li", "session-branch");
  const { item } = sessionRow(row, kids.length, family);
  branch.append(item);
  if (!kids.length) return branch;
  const nested = el("ul", "session-children");
  nested.dataset.treeParent = row.id;
  for (const kid of kids) {
    const child = sessionBranch(kid, children, seen, family);
    if (child) nested.append(child);
  }
  const toggle = buildToggle(row.id, row.title);
  nested.hidden = !(state.expandedFamilies.get(row.id) ?? true);
  item.firstElementChild.replaceWith(toggle);
  branch.append(nested);
  return branch;
}
