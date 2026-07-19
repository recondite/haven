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
import hashlib
import re

from haven import config

_WIKI_DIR = config.SECONDBRAIN_DIR / "wiki"
_WORD_RE = re.compile(r"[a-z0-9]+")
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)
_H1_RE = re.compile(r"^#\s+(.+)$", re.M)
_H2_RE = re.compile(r"^##\s+(.+)$", re.M)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _tokens(s: str) -> list[str]:
    return _WORD_RE.findall((s or "").lower())


def _strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1)


def _frontmatter_fields(text: str) -> dict:
    """Pull the freshness-relevant frontmatter fields (M2): updated, status, type.
    Missing fields come back None — pages predate strict schema in places."""
    m = _FRONTMATTER_RE.match(text)
    out = {"updated": None, "status": None, "type": None}
    if not m:
        return out
    fm = m.group(1)
    for key in out:
        km = re.search(rf"^{key}:\s*(\S+)", fm, re.M)
        if km:
            out[key] = km.group(1).strip()
    return out


def _sections(body: str) -> list[dict]:
    """Split a page body into heading-bounded sections. The pre-##-content
    (title + lead) is section 'Summary'."""
    marks = [(m.start(), m.group(1).strip()) for m in _H2_RE.finditer(body)]
    out = []
    first = marks[0][0] if marks else len(body)
    lead = body[:first].strip()
    if lead:
        out.append({"heading": "Summary", "text": lead})
    for i, (start, heading) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        out.append({"heading": heading, "text": body[start:end].strip()})
    return out


def _best_section(body: str, terms: set[str]) -> dict:
    """The section with the most query-term hits; falls back to the lead."""
    secs = _sections(body)
    if not secs:
        return {"heading": "Summary", "text": body.strip()[:800]}
    scored = [(len(terms & set(_tokens(s["heading"])) ) * 3 + len(terms & set(_tokens(s["text"]))), s)
              for s in secs]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1] if scored[0][0] > 0 else secs[0]


def _age_days(updated: str | None) -> int | None:
    if not updated:
        return None
    try:
        return (datetime.date.today() - datetime.date.fromisoformat(updated)).days
    except ValueError:
        return None


def search(query: str, limit: int = 8, include_deprecated: bool = False) -> list[dict]:
    """Rank SecondBrain pages by term overlap with the query. Returns cited hits:
    {title, path (relative, the citation), score, excerpt, updated, status, age_days}."""
    q = set(_tokens(query))
    if not q or not _WIKI_DIR.is_dir():
        return []
    hits: list[dict] = []
    for md in _WIKI_DIR.rglob("*.md"):
        if md.name in ("log.md", "index.md"):
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        fm = _frontmatter_fields(text)
        # M2 freshness rule: deprecated pages never reach agents/answers. The
        # citation of a retired fact manufactures false confidence.
        if fm["status"] == "deprecated" and not include_deprecated:
            continue
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
            "updated": fm["updated"],
            "status": fm["status"],
            "age_days": _age_days(fm["updated"]),
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


# ─── Context packs (M2: what the dispatch agents ground in) ──────────────
def _fragment_from_page(rel_path: str, terms: set[str]) -> dict | None:
    """Load one page and return its best-matching section as a citable fragment.
    None if the page is missing, empty, or deprecated."""
    full = config.SECONDBRAIN_DIR / rel_path
    if not full.is_file():
        return None
    text = full.read_text(encoding="utf-8", errors="replace")
    fm = _frontmatter_fields(text)
    if fm["status"] == "deprecated":
        return None
    body = _strip_frontmatter(text)
    sec = _best_section(body, terms)
    if not sec["text"]:
        return None
    return {
        "path": rel_path,
        "heading": sec["heading"],
        "text": sec["text"],
        "updated": fm["updated"],
        "status": fm["status"],
        "age_days": _age_days(fm["updated"]),
        "sha": hashlib.sha256(sec["text"].encode("utf-8")).hexdigest()[:8],
    }


def context_pack(item: dict, budget_chars: int = 2400) -> dict:
    """Deterministic, call-time SecondBrain context for a triage item (M2).

    Two lookups, no persistent index (185 pages = milliseconds):
      1. sender -> their person page (via the roster's email match) — who is this
      2. subject/snippet keywords -> best topic page — what is this about
    Rules: deprecated never included; ambiguous resolution => NO context (never
    guessed); hard char budget for the serialized local model. Every fragment
    carries path#heading + updated + sha so the draft's evidence is pinnable.
    """
    from haven.spine import spine  # late import: avoids cycle at module load

    terms = set(_tokens(f"{item.get('subject') or ''} {item.get('snippet') or ''}"))
    fragments: list[dict] = []
    seen_paths: set[str] = set()

    # 1. Sender identity — deterministic email match only.
    m = _EMAIL_RE.search(item.get("sender") or item.get("from") or "")
    if m:
        person = spine.person_by_email(m.group(0))
        if person and person.get("secondbrain_page"):
            frag = _fragment_from_page(
                f"wiki/entities/people/{person['secondbrain_page']}.md", terms or {"summary"})
            if frag:
                frag["why"] = "sender"
                fragments.append(frag)
                seen_paths.add(frag["path"])

    # 2. Topic pages by subject/snippet keywords.
    if terms:
        for hit in search(" ".join(sorted(terms)), limit=3):
            if hit["path"] in seen_paths or hit["score"] < 4:  # low-score = noise, skip
                continue
            frag = _fragment_from_page(hit["path"], terms)
            if frag:
                frag["why"] = "topic"
                fragments.append(frag)
                seen_paths.add(frag["path"])
            if len(fragments) >= 3:
                break

    # Budget: keep whole fragments while they fit; truncate the last one.
    packed: list[dict] = []
    used = 0
    for f in fragments:
        room = budget_chars - used
        if room <= 200:
            break
        if len(f["text"]) > room:
            f = {**f, "text": f["text"][:room] + " […]"}
        packed.append(f)
        used += len(f["text"])

    return {
        "fragments": packed,
        "citations": [{"source": "secondbrain", "path": f["path"], "heading": f["heading"],
                       "updated": f["updated"], "status": f["status"],
                       "age_days": f["age_days"], "sha": f["sha"], "why": f["why"]}
                      for f in packed],
    }


def render_pack(pack: dict) -> str:
    """Prompt block for a context pack — numbered so the model can cite [n]."""
    if not pack["fragments"]:
        return ""
    lines = ["CONTEXT FROM SECONDBRAIN (Ayar's knowledge wiki). Ground your reply in "
             "this when relevant; cite facts as [n]. Never contradict it; never "
             "invent beyond it:"]
    for i, f in enumerate(pack["fragments"], 1):
        lines.append(f"[{i}] {f['path']}#{f['heading']}"
                     f"{' (updated ' + f['updated'] + ')' if f['updated'] else ''}:\n{f['text']}")
    return "\n\n".join(lines) + "\n\n"


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
