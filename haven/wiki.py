"""LLM-maintained curated wiki.

Pattern from the LLM Wiki idea: a persistent, compounding knowledge base where
the LLM does all the bookkeeping. Garth marks important emails; Haven hands the
email + the current wiki state + the schema to Claude; Claude returns the file
edits; we apply them and append a log entry.

Storage layout: `data/wiki/`
  SCHEMA.md             — schema (read by LLM every ingest)
  index.md              — content catalog (LLM updates on every ingest)
  log.md                — append-only audit (Haven appends, but content is LLM-suggested)
  people/<slug>.md
  companies/<slug>.md
  events/<slug>.md
  topics/<slug>.md
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from haven import config, llm

log = logging.getLogger(__name__)

WIKI_DIR = config.DATA_DIR / "wiki"
ALLOWED_FOLDERS = {"people", "companies", "events", "topics"}
SCHEMA_FILENAME = "SCHEMA.md"
INDEX_FILENAME = "index.md"
LOG_FILENAME = "log.md"

_SAFE_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9\-/]*\.md$")


def ensure_wiki() -> None:
    """Idempotent setup. The schema/index/log files are seeded on first run from the
    versions shipped under data/wiki/, so this just creates folders if missing.
    """
    WIKI_DIR.mkdir(parents=True, exist_ok=True)
    for folder in ALLOWED_FOLDERS:
        (WIKI_DIR / folder).mkdir(exist_ok=True)


def list_pages() -> list[Path]:
    """Every wiki MD file, sorted by relative path."""
    return sorted(WIKI_DIR.rglob("*.md"))


def load_state() -> dict[str, str]:
    """Read every wiki page into a {relative_path: content} dict for prompt injection."""
    state: dict[str, str] = {}
    for p in list_pages():
        rel = p.relative_to(WIKI_DIR).as_posix()
        try:
            state[rel] = p.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("Could not read %s: %s", p, e)
    return state


def is_safe_path(rel_path: str) -> bool:
    """Reject anything outside an allowed folder, with traversal sequences, or odd extensions.

    Allowed: index.md OR <folder>/<slug>.md where folder ∈ ALLOWED_FOLDERS.
    """
    rel = rel_path.replace("\\", "/").lstrip("/")
    if ".." in rel or rel.startswith("/"):
        return False
    if rel == INDEX_FILENAME:
        return True
    if rel == SCHEMA_FILENAME or rel == LOG_FILENAME:
        return False  # protected — LLM should never overwrite these directly
    parts = rel.split("/", 1)
    if len(parts) != 2:
        return False
    folder, name = parts
    if folder not in ALLOWED_FOLDERS:
        return False
    return bool(_SAFE_PATH_RE.match(rel))


def write_page(rel_path: str, content: str) -> Path:
    if not is_safe_path(rel_path):
        raise ValueError(f"Rejected unsafe wiki path: {rel_path!r}")
    target = WIKI_DIR / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def append_log(entry: str) -> None:
    """Append one block to log.md. `entry` should be the body of the block."""
    log_path = WIKI_DIR / LOG_FILENAME
    if not log_path.exists():
        log_path.write_text("# Wiki Log\n\n", encoding="utf-8")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"\n## [{ts}] ingest | {entry}\n")


def _build_ingest_prompt(
    schema: str,
    state: dict[str, str],
    item: dict[str, Any],
    body: str,
) -> str:
    if state:
        pages_block = "\n\n".join(
            f"--- BEGIN {path} ---\n{content}\n--- END {path} ---"
            for path, content in state.items()
        )
    else:
        pages_block = "(wiki is empty)"

    sender = f"{item.get('sender_name') or '?'} <{item.get('sender_email') or '?'}>"
    company = item.get("sender_company") or item.get("sender_domain") or ""
    role = (item.get("garth_recipient_role") or "?").upper()
    body_excerpt = (body or "")[:5000]

    return f"""You are maintaining a personal wiki for Garth Thompson, CIO at Ayar Labs.
Read the SCHEMA carefully — it tells you the file structure, the templates, and
the conventions to follow on every ingest.

=== WIKI SCHEMA ===
{schema}

=== CURRENT WIKI STATE ===
{pages_block}

=== NEW SOURCE TO INGEST ===
Type: gmail email
From: {sender} ({company})
Garth recipient role: {role}
Subject: {item.get('subject') or '(no subject)'}
Date: {item.get('date') or ''}
LLM score: tag={item.get('tag')} urgency={item.get('urgency')} reply_needed={item.get('reply_needed')}
LLM summary: {item.get('summary') or ''}
Suggested action: {item.get('suggested_action') or ''}

--- BODY (first 5000 chars) ---
{body_excerpt}

=== TASK ===
Update the wiki to integrate this source. Decide which pages to create or update.
Bias toward DENSE pages over many thin ones — do not create a page for every name
that appears in passing. Only create pages for entities Garth will refer back to.

Output ONLY a JSON object with this exact shape (no markdown fences, no other text):
{{
  "files": [
    {{ "path": "people/lisa-dulchinos.md", "content": "<full new file content>" }},
    {{ "path": "events/2026-q2-audit-committee.md", "content": "..." }},
    {{ "path": "index.md", "content": "<updated index>" }}
  ],
  "log_entry": "<one-line summary suitable for log.md>"
}}

Rules:
- Provide the FULL content of each file you list, not a diff.
- For pages that already exist (see CURRENT WIKI STATE), preserve prior content and add new info as a dated subsection under "Recent activity" or similar. Do not silently rewrite history.
- File paths must match the schema (people/, companies/, events/, topics/, or index.md).
- Use lowercase dash-separated slugs (e.g. lisa-dulchinos.md).
- Cross-link with [[wikilinks]] — basename only, no .md extension.
- Bump `last_updated` in YAML frontmatter on any updated page.
- If a page does not need to change, do NOT include it in `files`.
- Always include an updated `index.md` if any pages were created.
- Do NOT include SCHEMA.md or log.md in `files`.
"""


async def ingest_source(item: dict[str, Any], body_text: str) -> dict[str, Any]:
    """Run a single ingest. Returns {files_written, log_entry, errors}."""
    ensure_wiki()
    schema_path = WIKI_DIR / SCHEMA_FILENAME
    schema = schema_path.read_text(encoding="utf-8") if schema_path.exists() else ""
    state = load_state()

    prompt = _build_ingest_prompt(schema, state, item, body_text)

    # Use the more capable model — ingest is heavy and infrequent.
    result = await llm.claude_json(prompt, model=config.LLM_MODEL, timeout=180.0)

    files = result.get("files") if isinstance(result, dict) else None
    if not isinstance(files, list):
        raise RuntimeError(f"Wiki ingest returned no 'files' array: {result!r}"[:500])

    written: list[str] = []
    errors: list[dict[str, str]] = []
    for f in files:
        if not isinstance(f, dict):
            continue
        path = f.get("path")
        content = f.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            errors.append({"path": str(path), "error": "missing path or content"})
            continue
        clean = path.replace("\\", "/").lstrip("/")
        if not is_safe_path(clean):
            errors.append({"path": clean, "error": "unsafe path; rejected"})
            continue
        try:
            write_page(clean, content)
            written.append(clean)
        except Exception as e:
            errors.append({"path": clean, "error": str(e)})

    log_entry = (
        result.get("log_entry")
        if isinstance(result, dict)
        else None
    ) or f"Ingested '{item.get('subject', '(no subject)')}'"
    sender_label = f"{item.get('sender_name') or item.get('sender_email') or '?'}"
    log_body = f"{log_entry}\n- From: {sender_label}\n- Files touched: {', '.join(written) if written else '(none)'}"
    if errors:
        log_body += f"\n- Errors: {errors}"
    append_log(log_body)

    return {
        "files_written": written,
        "log_entry": log_entry,
        "errors": errors,
    }
