/* Context component web UI: compaction timeline entries and /compact command. */

export const COMPACTION_TYPES = new Set([
  "context/compaction_handoff",
  "context/compaction_failed",
  "context/nothing_to_compact",
]);

function walkEntry(entry, origin = null, out = []) {
  if (!entry || typeof entry !== "object") return out;
  const effective = entry.origin || origin;
  out.push([entry, effective]);
  if (Array.isArray(entry.entries)) {
    for (const child of entry.entries) {
      walkEntry(child, effective, out);
    }
  }
  return out;
}

export function compactionSummary(record) {
  const entry = record?.entry && typeof record.entry === "object"
    ? record.entry
    : (typeof record?.type === "string" ? record : {});
  const parts = [];
  for (const [node, effective] of walkEntry(entry)) {
    if (node.type === "chat/text" && effective === "model" && node.fields?.text) {
      parts.push(node.fields.text);
    }
  }
  if (parts.length) return parts.join("\n\n");
  return entry.fields?.summary || "";
}

function formatTokens(count) {
  const value = Number(count) || 0;
  return value >= 1000 ? `${Math.round(value / 1000)}k` : String(value);
}

export function register(host) {
  const dom = host.dom || {};
  const entries = host.entries || {};
  const format = dom.tokens || formatTokens;

  host.entry("context/compaction_handoff", (record, ctx = {}) => {
    const el = dom.el || ctx.el;
    const fieldsOf = entries.fieldsOf || ctx.entries?.fieldsOf;
    const fields = fieldsOf ? fieldsOf(record) : (record?.entry?.fields || {});
    return el(
      "div",
      "divider",
      `Compacted ${format(fields.tokens_before)} → ${format(fields.tokens_after)}`,
    );
  });

  host.entry("context/compaction_failed", (record, ctx = {}) => {
    const el = dom.el || ctx.el;
    const modelFailure = entries.modelFailure || ctx.entries?.modelFailure;
    const failure = modelFailure ? modelFailure(record) : null;
    if (failure && dom.modelFailureView) return dom.modelFailureView(failure);
    return el("div", "divider", "Compaction failed");
  });

  host.entry("context/nothing_to_compact", () => null);

  host.command("/compact", async ({ value, session }) => {
    if (dom.state?.newSession || !session) {
      throw new Error("Send the first message to use this session command.");
    }
    await host.api.change("session/compact", { session, instructions: value });
  });
}
