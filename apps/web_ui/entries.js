/* Pure-object helpers for reading Protocol 8 native Entry wire records.
 * Zero DOM dependencies so unit tests can import this module directly in Node.
 */

export const NOTE_TYPES = new Set([
  "sessions/harness_note",
  "sessions/fork_note",
  "context/compaction_notice",
]);

export const UNDRAWN_TYPES = new Set([
  "sessions/system_prompt",
  "sessions/step",
]);

export function entryOf(record) {
  if (!record || typeof record !== "object") return {};
  if (record.entry && typeof record.entry === "object") return record.entry;
  return typeof record.type === "string" ? record : {};
}

export function entryType(record) {
  return entryOf(record).type || null;
}

export function fieldsOf(record) {
  const fields = entryOf(record).fields;
  return fields && typeof fields === "object" ? fields : {};
}

export function field(record, name, fallback = undefined) {
  const fields = fieldsOf(record);
  return Object.prototype.hasOwnProperty.call(fields, name)
    ? fields[name] : fallback;
}

export function linksOf(record) {
  const links = entryOf(record).links;
  return Array.isArray(links) ? links : [];
}

export function linkedTo(record, relation) {
  for (const link of linksOf(record)) {
    if (link && link.relation === relation) return link.target_id || null;
  }
  return null;
}

export function childrenOf(record) {
  const children = entryOf(record).entries;
  return Array.isArray(children) ? children : [];
}

export function* walkEntry(entry, origin = null) {
  if (!entry || typeof entry !== "object") return;
  const effective = entry.origin || origin;
  yield [entry, effective];
  if (Array.isArray(entry.entries)) {
    for (const child of entry.entries) {
      yield* walkEntry(child, effective);
    }
  }
}

export function combinedText(record, kind = "chat/text", origin = null) {
  const parts = [];
  for (const [node, effective] of walkEntry(entryOf(record))) {
    if (node.type !== kind) continue;
    if (origin !== null && effective !== origin) continue;
    const text = node.fields?.text;
    if (text) parts.push(text);
  }
  return parts.join("\n\n");
}

export function assistantText(record) {
  return combinedText(record, "chat/text", "model");
}

export function assistantReasoning(record) {
  return combinedText(record, "chat/reasoning", "model");
}

export function toolCalls(record) {
  const found = [];
  for (const [node, effective] of walkEntry(entryOf(record))) {
    if (node.type === "tools/call" && effective === "model") found.push(node);
  }
  return found;
}

export function callId(call) {
  if (!call || typeof call !== "object") return "";
  return call.fields?.call_id || call.id || "";
}

export function callName(call) {
  return call?.fields?.name || "";
}

export function callArguments(call) {
  return call?.fields?.arguments || "";
}

export function isContinue(record) {
  return entryType(record) === "sessions/notice" && Boolean(field(record, "continue"));
}

export function isNotice(record) {
  if (isContinue(record)) return false;
  if (record && record.notice_source) return true;
  return entryType(record) === "sessions/notice";
}

export function isTurnInput(record) {
  const kind = entryType(record);
  return kind === "chat/user_message" || kind === "sessions/notice";
}

export function isTurnStart(record) {
  return isTurnInput(record) && !field(record, "steer");
}

export function messageText(record) {
  if (entryType(record) === "sessions/notice") {
    for (const child of childrenOf(record)) {
      if (child && child.origin === "tool"
          && child.fields && Object.prototype.hasOwnProperty.call(child.fields, "text")) {
        return child.fields.text || "";
      }
    }
  }
  return field(record, "text", "") || "";
}

export function askId(record) {
  const kind = entryType(record);
  if (kind === "sessions/ask") {
    return field(record, "ask") || entryOf(record).id || null;
  }
  if (kind === "sessions/answer") {
    return field(record, "ask") || linkedTo(record, "answers");
  }
  return field(record, "ask", null);
}

export function toolCallId(record) {
  const kind = entryType(record);
  if (kind === "tools/result") {
    return field(record, "call") || linkedTo(record, "answers");
  }
  if (kind === "sessions/ask") {
    return field(record, "call") || linkedTo(record, "asks_about");
  }
  return field(record, "call", null);
}

export function interaction(record) {
  const value = field(record, "interaction");
  return value && typeof value === "object" ? value : null;
}

export const ATTACHMENT_TYPE = "attachments/file";

export function modelFailure(record) {
  const value = field(record, "model_failure");
  return value && typeof value === "object" ? value : null;
}

export function attachmentsOf(record) {
  const found = [];
  const seen = new Set();
  for (const [node] of walkEntry(entryOf(record))) {
    if (node.type !== ATTACHMENT_TYPE) continue;
    const fields = node.fields;
    if (!fields || typeof fields !== "object") continue;
    const id = typeof fields.attachment_id === "string" ? fields.attachment_id : "";
    const path = typeof fields.path === "string" ? fields.path : "";
    const key = id || (path ? `path:${path}` : "");
    if (!key || seen.has(key)) continue;
    seen.add(key);
    found.push({
      attachment_id: id,
      name: fields.name || "file",
      media_type: fields.media_type || "application/octet-stream",
      size_bytes: typeof fields.size_bytes === "number" ? fields.size_bytes : 0,
      sha256: fields.sha256 || "",
      delivery: fields.delivery || "inline",
      path,
      origin: fields.origin || node.origin || "",
      kind: fields.kind || "",
      title: fields.title || "",
      source_path: fields.source_path || "",
      duration_seconds: fields.duration_seconds,
      width: fields.width,
      height: fields.height,
      pages: fields.pages,
    });
  }
  return found;
}

export function searchSuggestions(record) {
  const outer = fieldsOf(record);
  const inner = outer.fields && typeof outer.fields === "object"
    ? outer.fields : {};
  const rawQueries = inner.web_search_queries ?? outer.web_search_queries;
  const queries = Array.isArray(rawQueries)
    ? rawQueries.filter((q) => typeof q === "string" && q.trim())
    : [];
  const rawHtml = inner.search_suggestions_html ?? outer.search_suggestions_html;
  const html = typeof rawHtml === "string" && rawHtml.trim() ? rawHtml : "";
  if (!queries.length && !html) return null;
  return { queries, html };
}
