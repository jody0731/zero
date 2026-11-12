"""The search over the tools a session can call but doesn't send: each tool
a document of its name's words, whose it is, its description and its
arguments' names (Hermes': a schema's body adds noise, not recall), ranked
by BM25 (Codex's, OpenClaw's, Pi's and Hermes' measure).

Two floors keep a miss short rather than full of near misses (Hermes'
findings, its floors loosened by a measure): a tool must carry one of the
query's two rarest words that some tool carries, those that name what is
wanted ("rust" and "compiler" in "rust compiler version of a binary", not
"version"); and a query of COVERAGE_FROM_WORDS such words or more needs half
of them, so a hunt whose words are each somewhere finds nothing rather than
a tool that shares one. Hermes' own gate, the rarest word whether or not a
tool carries it, put the right tool first for 12 of 17 plain queries over an
MCP server's 229 tools, these floors for 14: a word that names the wrong
tool, or none, is passed over. A word that is a tool's name, whole or as its
source names it, finds that tool first.

No package: the words are cut to a rough stem by a few English endings, and
the counts are kept per search, since the tools change as servers come and
go and a search is rare.
"""

import math
import re

# BM25's usual constants: how fast a word's weight saturates, and how much
# a long document's words count less
K1 = 1.2
B = 0.75
# How many of the query's rarest words a tool must carry one of (among
# those some tool carries)
RAREST_WORDS = 2
# From how many of the query's answerable words a tool must carry half
COVERAGE_FROM_WORDS = 4
COVERAGE_SHARE = 0.5
# A word, a name's part: an acronym ("HTML" in "HTMLParser"), a word with
# its capital ("Hint" in "readOnlyHint"), a number
WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Za-z][a-z]*|[0-9]+")
# Words that say nothing of a tool
STOP_WORDS = frozenset(
    "a an and are as at be by for from in into is it its of on or that the "
    "this to with".split())


def stem(word):
    """A word cut to a rough stem by a few English endings: "files" and
    "file", "reading" and "read", "listed" and "list" meet."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    for ending in ("sses", "xes", "ches", "shes"):
        if word.endswith(ending):
            return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    return word


def words(text):
    """A text's words as the search counts them: split at every mark and
    between a name's parts (snake_case, camelCase), lower case, stemmed,
    the words that say nothing left out."""
    found = []
    for part in WORD.findall(text or ""):
        word = part.lower()
        if word not in STOP_WORDS:
            found.append(stem(word))
    return found


class Document:
    """One tool as the search reads it. `names`: the names a query word
    finds it by whole (its name, and its name at its source)."""

    def __init__(self, name, names, text):
        self.name = name
        self.names = {n.lower() for n in names}
        self.words = words(text)
        self.counts = {}
        for word in self.words:
            self.counts[word] = self.counts.get(word, 0) + 1


def document(name, entry):
    """The document of a found tool (core's registry entry): its names, then
    whose it is, its description and its arguments' names."""
    function = entry["schema"]["function"]
    at_source = entry.get("name_at_source") or name
    properties = (function.get("parameters") or {}).get("properties") or {}
    text = " ".join([at_source, entry.get("source") or "",
                     function.get("description") or "", *properties])
    return Document(name, (name, at_source), text)


def search(documents, query, count):
    """The names of the `count` documents that best answer `query`, best
    first: those a query word names whole, then by BM25 among those that
    pass the floors; [] where none does."""
    asked = [part.strip(" ,;") for part in (query or "").split()]
    named = []
    for word in asked:
        for doc in documents:
            if word.lower() in doc.names and doc.name not in named:
                named.append(doc.name)
    query_words = [w for w in words(query) if w]
    ranked = rank(documents, query_words, set(named))
    return (named + ranked)[:count]


def rank(documents, query_words, leave_out):
    """BM25 over the documents that carry one of the query's rarest words
    and enough of its answerable words, `leave_out` aside."""
    if not documents or not query_words:
        return []
    total = len(documents)
    holding = {}
    for doc in documents:
        for word in set(doc.words):
            holding[word] = holding.get(word, 0) + 1

    def weight(word):
        held = holding.get(word, 0)
        return math.log(1 + (total - held + 0.5) / (held + 0.5))

    answerable = {w for w in query_words if holding.get(w)}
    if not answerable:
        return []
    rarest = set(sorted(answerable, key=weight, reverse=True)[:RAREST_WORDS])
    needed = 1
    if len(answerable) >= COVERAGE_FROM_WORDS:
        needed = math.ceil(len(answerable) * COVERAGE_SHARE)
    average = sum(len(doc.words) for doc in documents) / total or 1
    scored = []
    for doc in documents:
        if doc.name in leave_out or not rarest & doc.counts.keys():
            continue
        if sum(1 for w in answerable if w in doc.counts) < needed:
            continue
        score = 0.0
        for word in query_words:
            seen = doc.counts.get(word, 0)
            if seen:
                norm = K1 * (1 - B + B * len(doc.words) / average)
                score += weight(word) * seen * (K1 + 1) / (seen + norm)
        scored.append((score, doc.name))
    scored.sort(key=lambda pair: -pair[0])
    return [name for _, name in scored]
