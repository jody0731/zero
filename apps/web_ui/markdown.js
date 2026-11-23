/* Markdown, as the page reads a reply: blocks and spans, never HTML.
 *
 * ZERO writes Markdown (headings, bold, lists, fenced code); the transcript
 * shows it as written, but the page builds its DOM with textContent, so this
 * file only recognises the structure. `blocks(text)` gives a list of
 * `{type: "heading"|"paragraph"|"code"|"list"|"quote"|"rule"|"table", ...}`
 * (a table: `head`, a row of cells, and `rows`, each cell its spans) and each
 * text block carries spans: `{kind: "text"|"code"|"strong"|"em"|"del"|"link"}`
 * where the nesting kinds have their own `spans`, and a link has its `href`.
 *
 * Only http and https links are links: a `javascript:` URL or a bare path
 * stays the text it was written as. An unclosed fence runs to the end.
 *
 * `splitBlockChunks(text)` splits a document into fence-aware top-level raw
 * chunks so `patchMarkdown(container, text)` can memoize completed prefix
 * blocks in place during 10 Hz streaming without touching earlier DOM nodes.
 */

const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^(\s*)[-*+]\s+(.*)$/;
const NUMBERED = /^(\s*)\d+[.)]\s+(.*)$/;
const FENCE = /^\s*(```+|~~~+)\s*([\w+#.-]*)\s*$/;
const RULE = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const QUOTE = /^\s*>\s?(.*)$/;
const LINK = /^\[([^\]]+)\]\(([^)\s]+)\)/;
// A table's lines start with a pipe; its second line is the dashes' row
const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_RULE = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;
const SCHEMES = ["http://", "https://"];

function isClosingFence(line, fenceChar, fenceLen) {
  const trimmed = line.trim();
  if (trimmed.length < fenceLen) return false;
  for (let i = 0; i < trimmed.length; i += 1) {
    if (trimmed[i] !== fenceChar) return false;
  }
  return true;
}

export function splitBlockChunks(text) {
  if (typeof text !== "string" || !text.trim()) return [];
  const lines = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
  const chunks = [];
  let paragraph = [];
  let at = 0;

  function flush() {
    if (!paragraph.length) return;
    const raw = paragraph.join("\n");
    paragraph = [];
    if (raw.trim()) chunks.push(raw);
  }

  while (at < lines.length) {
    const line = lines[at];
    const fence = line.match(FENCE);
    if (fence) {
      flush();
      const fenceChar = fence[1][0];
      const fenceLen = fence[1].length;
      const start = at;
      at += 1;
      while (at < lines.length && !isClosingFence(lines[at], fenceChar, fenceLen)) {
        at += 1;
      }
      if (at < lines.length) at += 1;
      chunks.push(lines.slice(start, at).join("\n"));
      continue;
    }
    if (HEADING.test(line) || RULE.test(line)) {
      flush();
      chunks.push(line);
      at += 1;
      continue;
    }
    if (TABLE_ROW.test(line) && at + 1 < lines.length
        && TABLE_RULE.test(lines[at + 1])) {
      flush();
      const start = at;
      at += 2;
      while (at < lines.length && TABLE_ROW.test(lines[at])) at += 1;
      chunks.push(lines.slice(start, at).join("\n"));
      continue;
    }
    const bullet = line.match(BULLET);
    const numbered = line.match(NUMBERED);
    if (bullet || numbered) {
      flush();
      const ordered = Boolean(numbered);
      const start = at;
      while (at < lines.length) {
        const one = ordered ? lines[at].match(NUMBERED) : lines[at].match(BULLET);
        if (!one) break;
        at += 1;
      }
      chunks.push(lines.slice(start, at).join("\n"));
      continue;
    }
    if (QUOTE.test(line)) {
      flush();
      const start = at;
      while (at < lines.length && QUOTE.test(lines[at])) at += 1;
      chunks.push(lines.slice(start, at).join("\n"));
      continue;
    }
    if (!line.trim()) {
      flush();
      at += 1;
      continue;
    }
    paragraph.push(line);
    at += 1;
  }
  flush();
  return chunks;
}

export function blocks(text) {
  if (typeof text !== "string" || !text.trim()) return [];
  const lines = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
  const found = [];
  let paragraph = [];
  let at = 0;

  function flush() {
    if (!paragraph.length) return;
    const joined = paragraph.join(" ").replace(/\s+/g, " ").trim();
    paragraph = [];
    if (joined) found.push({ type: "paragraph", spans: inline(joined) });
  }

  while (at < lines.length) {
    const line = lines[at];
    const fence = line.match(FENCE);
    if (fence) {
      flush();
      const fenceChar = fence[1][0];
      const fenceLen = fence[1].length;
      const lang = fence[2] || "";
      at += 1;
      const body = [];
      while (at < lines.length && !isClosingFence(lines[at], fenceChar, fenceLen)) {
        body.push(lines[at]);
        at += 1;
      }
      at += 1;
      found.push({ type: "code", lang, text: body.join("\n") });
      continue;
    }
    const heading = line.match(HEADING);
    if (heading) {
      flush();
      found.push({
        type: "heading",
        level: heading[1].length,
        spans: inline(heading[2].trim()),
      });
      at += 1;
      continue;
    }
    if (RULE.test(line)) {
      flush();
      found.push({ type: "rule" });
      at += 1;
      continue;
    }
    if (TABLE_ROW.test(line) && at + 1 < lines.length
        && TABLE_RULE.test(lines[at + 1])) {
      flush();
      const head = cells(line);
      const rows = [];
      at += 2;
      while (at < lines.length && TABLE_ROW.test(lines[at])) {
        rows.push(cells(lines[at]));
        at += 1;
      }
      found.push({ type: "table", head, rows });
      continue;
    }
    const bullet = line.match(BULLET);
    const numbered = line.match(NUMBERED);
    if (bullet || numbered) {
      flush();
      const ordered = Boolean(numbered);
      const items = [];
      while (at < lines.length) {
        const one = ordered ? lines[at].match(NUMBERED) : lines[at].match(BULLET);
        if (!one) break;
        items.push(inline(one[2].trim()));
        at += 1;
      }
      found.push({ type: "list", ordered, items });
      continue;
    }
    const quote = line.match(QUOTE);
    if (quote) {
      flush();
      const quoted = [];
      while (at < lines.length) {
        const more = lines[at].match(QUOTE);
        if (!more) break;
        quoted.push(more[1].trim());
        at += 1;
      }
      found.push({ type: "quote", spans: inline(quoted.join(" ").trim()) });
      continue;
    }
    if (!line.trim()) {
      flush();
      at += 1;
      continue;
    }
    paragraph.push(line);
    at += 1;
  }
  flush();
  return found;
}

function cells(line) {
  const inner = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return inner.split("|").map((cell) => inline(cell.trim()));
}

export function inline(text) {
  const spans = [];
  let plain = "";
  let at = 0;

  function keep() {
    if (plain) {
      spans.push({ kind: "text", text: plain });
      plain = "";
    }
  }

  while (at < text.length) {
    const rest = text.slice(at);
    if (rest[0] === "`") {
      const end = text.indexOf("`", at + 1);
      if (end > at) {
        keep();
        spans.push({ kind: "code", text: text.slice(at + 1, end) });
        at = end + 1;
        continue;
      }
    }
    if (rest.startsWith("**") || rest.startsWith("__")) {
      const marker = rest.slice(0, 2);
      const end = text.indexOf(marker, at + 2);
      if (end > at + 2) {
        keep();
        spans.push({ kind: "strong", spans: inline(text.slice(at + 2, end)) });
        at = end + 2;
        continue;
      }
    }
    if (rest.startsWith("~~")) {
      const end = text.indexOf("~~", at + 2);
      if (end > at + 2) {
        keep();
        spans.push({ kind: "del", spans: inline(text.slice(at + 2, end)) });
        at = end + 2;
        continue;
      }
    }
    if (rest[0] === "*" || rest[0] === "_") {
      const marker = rest[0];
      if (mayOpen(text, at, marker)) {
        const end = text.indexOf(marker, at + 1);
        if (end > at + 1 && mayClose(text, end, marker)) {
          keep();
          spans.push({ kind: "em", spans: inline(text.slice(at + 1, end)) });
          at = end + 1;
          continue;
        }
      }
    }
    const link = rest.match(LINK);
    if (link && SCHEMES.some((scheme) => link[2].startsWith(scheme))) {
      keep();
      spans.push({
        kind: "link",
        text: link[1],
        href: link[2],
        spans: inline(link[1]),
      });
      at += link[0].length;
      continue;
    }
    plain += rest[0];
    at += 1;
  }
  keep();
  return spans;
}

function mayOpen(text, at, marker) {
  if (at + 1 >= text.length || /\s/.test(text[at + 1])) return false;
  if (marker === "_" && at > 0 && /[A-Za-z0-9]/.test(text[at - 1])) return false;
  return true;
}

function mayClose(text, at, marker) {
  if (at === 0 || /\s/.test(text[at - 1])) return false;
  if (marker === "_" && at + 1 < text.length
      && /[A-Za-z0-9]/.test(text[at + 1])) return false;
  return true;
}

function makeEl(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function spanNodes(list) {
  return (list || []).map((span) => {
    if (span.kind === "code") return makeEl("code", null, span.text);
    if (span.kind === "link") {
      const a = makeEl("a");
      a.href = span.href;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      if (span.spans) a.append(...spanNodes(span.spans));
      else a.textContent = span.text || span.href;
      return a;
    }
    if (["strong", "em", "del"].includes(span.kind)) {
      const node = makeEl(span.kind);
      node.append(...spanNodes(span.spans));
      return node;
    }
    return document.createTextNode(span.text || "");
  });
}

export function blockNode(block) {
  if (block.type === "heading") {
    const node = makeEl(`h${Math.min(Math.max(block.level || 1, 1), 6)}`);
    node.append(...spanNodes(block.spans));
    return node;
  }
  if (block.type === "code") {
    const pre = makeEl("pre");
    pre.append(makeEl("code", null, block.text));
    return pre;
  }
  if (block.type === "list") {
    const list = makeEl(block.ordered ? "ol" : "ul");
    for (const item of block.items || []) {
      const li = makeEl("li");
      li.append(...spanNodes(item));
      list.append(li);
    }
    return list;
  }
  if (block.type === "quote") {
    const quote = makeEl("blockquote");
    quote.append(...spanNodes(block.spans));
    return quote;
  }
  if (block.type === "rule") return makeEl("hr");
  if (block.type === "table") {
    const wrap = makeEl("div", "table");
    const table = makeEl("table");
    const head = makeEl("tr");
    for (const cell of block.head) {
      const th = makeEl("th");
      th.append(...spanNodes(cell));
      head.append(th);
    }
    table.append(head);
    for (const row of block.rows) {
      const tr = makeEl("tr");
      for (const cell of row) {
        const td = makeEl("td");
        td.append(...spanNodes(cell));
        tr.append(td);
      }
      table.append(tr);
    }
    wrap.append(table);
    return wrap;
  }
  const p = makeEl("p");
  p.append(...spanNodes(block.spans));
  return p;
}

const blockCache = new WeakMap();

export function patchMarkdown(container, text) {
  const prevRaws = blockCache.get(container) || [];
  const nextRaws = splitBlockChunks(text);
  if (!nextRaws.length) {
    const fallback = text || "";
    if (prevRaws.length === 1 && prevRaws[0] === fallback
        && container.children.length === 1) {
      return container;
    }
    container.replaceChildren(makeEl("p", null, fallback));
    blockCache.set(container, [fallback]);
    return container;
  }
  let prefix = 0;
  const maxPrefix = Math.min(prevRaws.length, nextRaws.length,
                             container.children.length);
  while (prefix < maxPrefix && prevRaws[prefix] === nextRaws[prefix]) {
    prefix += 1;
  }
  for (let i = prefix; i < nextRaws.length; i += 1) {
    const parsed = blocks(nextRaws[i]);
    const nextNode = parsed.length
      ? blockNode(parsed[0]) : makeEl("p", null, nextRaws[i]);
    const existing = container.children[i];
    if (existing) existing.replaceWith(nextNode);
    else container.append(nextNode);
  }
  while (container.children.length > nextRaws.length) {
    container.lastElementChild.remove();
  }
  blockCache.set(container, nextRaws);
  return container;
}

export function renderMarkdown(text) {
  const wrap = makeEl("div", "reply");
  patchMarkdown(wrap, text);
  return wrap;
}
