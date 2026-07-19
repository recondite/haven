"""Document ingest (M5/M6, build plan v2): local uploads + Google Docs links →
schema-valid SecondBrain source-page DRAFTS, through the existing approval gate.

Pipeline: raw bytes → text extraction → LLM structuring into one type:source
page → knowledge.build_page → executor.validate_wiki → spine draft (kind=wiki).
Nothing writes to SecondBrain without GT's Approve; the raw file is preserved
append-only so a bad extraction is visible at the gate, never silent.
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import re

from haven import config, executor, knowledge, runtime
from haven.spine import spine

log = logging.getLogger("haven")

UPLOAD_DIR = config.DATA_DIR / "uploads"
MAX_BYTES = 15 * 1024 * 1024
_LLM_CHAR_BUDGET = 12000        # cap on text handed to the local model
SUPPORTED = (".docx", ".pdf", ".txt", ".md")


class IngestError(Exception):
    pass


# ─── extraction ──────────────────────────────────────────
def extract_text(filename: str, data: bytes) -> str:
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in SUPPORTED:
        raise IngestError(f"unsupported type {ext or '(none)'} — supported: {', '.join(SUPPORTED)}")
    if ext in (".txt", ".md"):
        return data.decode("utf-8", errors="replace")
    if ext == ".docx":
        import io

        from docx import Document
        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if ext == ".pdf":
        import io

        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
        except Exception as e:  # noqa: BLE001 — bad PDF is a clear error, never an empty page
            raise IngestError(f"could not read PDF: {e}")
    raise IngestError(f"no extractor for {ext}")  # unreachable


def _store_raw(filename: str, data: bytes) -> tuple[str, str]:
    """Persist raw bytes append-only; return (relative_path, sha8)."""
    sha8 = hashlib.sha256(data).hexdigest()[:8]
    today = datetime.date.today()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)[:80] or "upload"
    sub = UPLOAD_DIR / f"{today.year:04d}" / f"{today.month:02d}"
    sub.mkdir(parents=True, exist_ok=True)
    dest = sub / f"{sha8}-{safe}"
    if not dest.exists():
        dest.write_bytes(data)
    return str(dest.relative_to(config.DATA_DIR)).replace("\\", "/"), sha8


# ─── structuring → draft ─────────────────────────────────
async def _structure_to_page(title_hint: str, text: str, provenance: dict) -> tuple[str, str]:
    """LLM turns extracted text into ONE schema-valid source page. Returns
    (page_markdown, target_path). Prompt forbids invention; page cites the raw."""
    truncated = len(text) > _LLM_CHAR_BUDGET
    body_in = text[:_LLM_CHAR_BUDGET]
    prompt = (
        "You are cataloguing a document into a knowledge wiki. From the document "
        "text below, produce a concise wiki page body in markdown with:\n"
        "- a one-line **tl;dr**\n- a short **## Summary**\n- a **## Key facts** "
        "bullet list of the concrete facts (names, numbers, dates, decisions)\n"
        "Use ONLY what the text supports; do not invent. No title heading (added "
        "separately). Keep under 400 words.\n\nDOCUMENT TEXT:\n" + body_in
    )
    try:
        body = (await runtime.call(prompt, timeout=180)).strip()
    except Exception as e:  # noqa: BLE001
        raise IngestError(f"structuring failed: {e}")
    if truncated:
        body += f"\n\n_[truncated — full document at `data/{provenance.get('raw_path','?')}`]_"
    prov = "\n".join(f"- {k}: {v}" for k, v in provenance.items() if v)
    body += f"\n\n## Provenance\n{prov}\n"
    title = title_hint.strip() or "Untitled document"
    target = knowledge.ingest_target(title, "source")
    page = knowledge.build_page(title, "source", ["haven-ingest", "document"], body)
    return page, target


async def ingest_document(title_hint: str, filename: str, data: bytes,
                          origin: str, extra_prov: dict | None = None) -> dict:
    """Full pipeline for a byte payload (upload or fetched Doc). Creates a
    pending wiki draft; returns {draft_id, target, duplicate_warning}."""
    if len(data) > MAX_BYTES:
        raise IngestError(f"file too large ({len(data)} bytes; cap {MAX_BYTES})")
    text = extract_text(filename, data)
    if not text.strip():
        raise IngestError("no extractable text in document")
    raw_path, sha8 = _store_raw(filename, data)
    provenance = {"origin": origin, "filename": filename, "sha": sha8,
                  "raw_path": raw_path, "ingested": datetime.date.today().isoformat(),
                  "uploader": "GT", **(extra_prov or {})}
    page, target = await _structure_to_page(title_hint or filename, text, provenance)
    try:
        executor.validate_wiki(page, target)
    except executor.ExecutorError as e:
        raise IngestError(f"structured page failed schema/validation: {e}")
    dups = knowledge.similar_pages(title_hint or filename)
    evidence = [{"source": "document", "filename": filename, "sha": sha8,
                 "raw_path": raw_path, "chars": len(text), "origin": origin}]
    if dups:
        evidence.append({"source": "warning",
                         "excerpt": "Possible duplicate of: "
                                    + " · ".join(f"{d['path']} ({int(d['overlap']*100)}%)" for d in dups)})
    draft_id = spine.create_draft(None, "wiki", target, page, evidence=evidence)
    return {"draft_id": draft_id, "target": target, "duplicate_warning": bool(dups),
            "chars": len(text), "truncated": len(text) > _LLM_CHAR_BUDGET}


def list_recent(limit: int = 20) -> list[dict]:
    """Recent document-sourced drafts across states, for the Ingest panel."""
    out = []
    for status in ("pending", "approved", "rejected"):
        for d in spine.list_drafts(status):
            ev = d.get("evidence")
            if ev and '"source": "document"' in ev:
                out.append({"id": d["id"], "target": d["target"], "status": status,
                            "created_at": d.get("created_at")})
    out.sort(key=lambda d: d["id"], reverse=True)
    return out[:limit]
