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
 */
"use strict";

var ZeroMarkdown = (function () {
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

  function blocks(text) {
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
        const marker = fence[1].slice(0, 3);
        const lang = fence[2] || "";
        at += 1;
        const body = [];
        while (at < lines.length && !lines[at].trim().startsWith(marker)) {
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
        found.push({ type: "heading", level: heading[1].length,
                     spans: inline(heading[2].trim()) });
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
          const one = ordered ? lines[at].match(NUMBERED)
                              : lines[at].match(BULLET);
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

  function inline(text) {
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
        spans.push({ kind: "link", text: link[1], href: link[2],
                     spans: inline(link[1]) });
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

  return { blocks, inline };
})();

if (typeof module !== "undefined" && module.exports) {
  module.exports = ZeroMarkdown;
}
