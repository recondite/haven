# CLAUDE.md — Haven

Guidance for working in `Projects/Haven/`. This is a **real Python codebase**, not a
documents folder. The general Cowork rules still apply, but the build/test workflow
and the Haven-specific ground rules below take precedence here.

## What Haven is

Real-time "action required" dashboard for Garth (CIO, Ayar Labs) — the successor to
the CIC dashboard. A FastAPI orchestrator polls several sources (Gmail, Slack,
Freshservice, Otter.ai), pre-filters and LLM-scores each item, and streams the
survivors to a single-page UI over SSE. Items can be marked done, snoozed, captured
to Linear, or ingested into an LLM-maintained wiki under `data/wiki/`.

Full detail: `README.md`. Read it before making changes.

## Read first, every time

1. `ground_rules/ground_rules.md` — **hard boundaries.** The big one: Haven must
   never delete or destructively modify anything in an external service (Gmail,
   Slack, JIRA, Linear, Otter, Freshservice) or in local `data/`. Archive/status
   transitions are allowed only on explicit user action. Secrets live only in
   `.env`. No autonomous outbound sends. Localhost-only by default.
2. `README.md` — architecture, sources, run/test commands.
3. `CODE_REVIEW.md` and `REFACTOR_NOTES.md` if present — current known issues and
   direction.

## Environment & commands

Package/venv manager is **uv** (not pip/poetry). Run from `Projects/Haven/`:

- Install: `uv sync` (add `--extra dev` for test/lint deps)
- Run: `uv run python -m haven` → http://127.0.0.1:8765/
- Test: `uv run pytest`
- Lint: `uv run ruff check .`

**Test before declaring done.** Per Garth's global rules and the nature of this
codebase — run `pytest` and `ruff` on any change to logic before presenting it as
complete. The pure logic (filters, enrichment, contacts, wiki, linear, llm) has unit
coverage; add/extend tests when you touch it.

## Layout (see README for the full map)

- `haven/` — app package: `main.py` (assembly), `config.py`, `db.py` (SQLite dedup),
  `filters.py`, `scoring.py` (LLM), `enrichment.py`, `linear.py`, `wiki.py`,
  `llm.py`, `routers/` (one APIRouter per concern), `services/`, `sources/`.
- `agents/*.yaml` — per-source poll config (`enabled: false` to disable a source).
- `data/` — local SQLite + LLM wiki; **append-only**, never unlink files.
- `.env` — secrets only; gitignored. Copy from `.env.example`.

## Working notes

- This folder is a git repo — check `git status`/diff before and after changes.
- Any change that could touch a destructive code path in an external service must be
  surfaced to Garth before implementing (see ground rule #1).
- Don't transmit anything externally or wire up autonomous sends without explicit
  review.
