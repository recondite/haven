"""Knowledge — SecondBrain-first retrieval with mandatory citation.

Plan v4 Phase 3: SecondBrain is the single Ayar knowledge store and the first
reference for every lookup ("SecondBrain first, live sources second, cite which").
Read-only here; ingest is approval-gated (see dispatch draft_ingest_wiki).

ponytail: term-overlap ranking over 185 markdown files, not embeddings. Title
and heading hits weigh more than body. Re-home to a vector index only if recall
on real queries proves insufficient.
"""
from __future__ import annotations

import datetime
import re

from haven import config

_WIKI_DIR = config.SECONDBRAIN_DIR / "wiki"
_WORD_RE = re.compile(r"[a-z0-9]+")
_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.S)
_H1_RE = re.compile(r"^#\s+(.+)$", re.M)


def _tokens(s: str) -> list[str]:
    return _WORD_RE.findall((s or "").lower())


def _strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1)


def search(query: str, limit: int = 8) -> list[dict]:
    """Rank SecondBrain pages by term overlap with the query. Returns cited hits:
    {title, path (relative, the citation), score, excerpt}."""
    q = set(_tokens(query))
    if not q or not _WIKI_DIR.is_dir():
        return []
    hits: list[dict] = []
    for md in _WIKI_DIR.rglob("*.md"):
        if md.name in ("log.md", "index.md"):
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        body = _strip_frontmatter(text)
        title_m = _H1_RE.search(body)
        title = title_m.group(1).strip() if title_m else md.stem
        body_toks = _tokens(body)
        if not body_toks:
            continue
        title_toks = set(_tokens(title))
        body_set = set(body_toks)
        # weight: title/exact-name hits count triple; body coverage counts once each.
        score = 3 * len(q & title_toks) + len(q & body_set)
        # small boost for the slug matching a query term (entity lookups)
        if q & set(_tokens(md.stem)):
            score += 2
        if score <= 0:
            continue
        hits.append({
            "title": title,
            "path": str(md.relative_to(config.SECONDBRAIN_DIR)).replace("\\", "/"),
            "score": score,
            "excerpt": _excerpt(body, q),
        })
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits[:limit]


def _excerpt(body: str, q: set[str], width: int = 220) -> str:
    """First window of body containing a query term, else the opening line."""
    low = body.lower()
    for term in q:
        i = low.find(term)
        if i >= 0:
            start = max(0, i - 60)
            return " ".join(body[start:start + width].split())
    return " ".join(body[:width].split())


# ─── Ingest (draft a schema-conformant page; write is approval-gated) ────
_TYPE_DIR = {
    "person": "wiki/entities/people", "company": "wiki/entities/companies",
    "team": "wiki/entities/teams", "concept": "wiki/concepts",
    "project": "wiki/projects", "tool": "wiki/it-stack",
    "source": "wiki/sources", "analysis": "wiki/analyses",
}


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-") or "untitled"


def ingest_target(title: str, type_: str) -> str:
    return f"{_TYPE_DIR.get(type_, 'wiki/sources')}/{slugify(title)}.md"


def build_page(title: str, type_: str, tags: list[str], body: str) -> str:
    """Assemble a SecondBrain-schema page. executor.validate_wiki is the gate;
    this just produces a conformant draft for the approval queue."""
    today = datetime.date.today().isoformat()
    tag_list = ", ".join(t.strip() for t in (tags or []) if t.strip())
    return (f"---\ntype: {type_}\ntags: [{tag_list}]\ncreated: {today}\nupdated: {today}\n"
            f"sources: [haven-ingest-{today}]\n---\n\n# {title}\n\n{body.strip()}\n")


def get_page(rel_path: str) -> str | None:
    """Read a page by its SecondBrain-relative path. Guards against traversal."""
    target = (config.SECONDBRAIN_DIR / rel_path).resolve()
    try:
        target.relative_to(config.SECONDBRAIN_DIR.resolve())
    except ValueError:
        return None  # outside SecondBrain — refuse
    if target.is_file() and target.suffix == ".md":
        return target.read_text(encoding="utf-8", errors="replace")
    return None
