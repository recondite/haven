# Haven — Refactor Notes

Companion to `CODE_REVIEW.md`. Behavior-preserving refactor of the `haven/`
package. The HTTP route table is unchanged (48 routes, verified by diff before/after)
and the pure-logic test suite (66 tests) passes.

## 1. Correctness fixes

- **Gmail token-refresh race.** `GmailAuth` now refreshes the OAuth token under
  an `asyncio.Lock` and writes the token file **atomically** (`tempfile` +
  `os.replace`), so the parallel metadata/full-fetch fan-out can no longer
  corrupt `gmail-token.json` or trigger N redundant refreshes. The built Gmail
  service is cached on `GmailAuth` and shared by every `GmailFetcher`, removing
  the ~4×-per-message client rebuild. `GmailFetcher._service()` and the former
  `_gmail_service()` in `main.py` both delegate here — one refresh path, no drift.
- **SQLite encapsulation break.** Added `CursorStore.list_rejected_ids(source)`
  (lock-guarded) and replaced the raw `cursor_store._conn.execute(...)` call in
  the noise-label handler, so every DB access now goes through the locked API.

## 2. Test harness

- `pytest` + `ruff` configured in `pyproject.toml` (`[project.optional-dependencies].dev`).
- `tests/` covers the pure functions the review flagged as highest-value:
  `filters` (rules, blocklist, watchlist), `enrichment` (company/role/thread-state/dates),
  `contacts.derive_contacts`, `llm._extract_json`, `wiki.is_safe_path`, and the
  Linear payload shapers. 66 tests.

## 3. Structure

- **Gmail poll pipeline extracted** from the route handler into
  `haven/services/gmail_poll.py` (`run(force)`), so the ~200-line Pass A–E
  orchestration is testable independently of HTTP.
- **`main.py` split** from 1,365 lines to ~226. It now only assembles the app:
  lifespan + scheduler, heartbeat, SSE, health, llm status/test, favicon, and
  `include_router(...)`. Every per-source endpoint moved to `haven/routers/*`
  (`gmail`, `slack`, `freshservice`, `otter`, `wiki`, `contacts`, `items`).
- **Shared singletons** moved to `haven/deps.py` (`gmail_auth`) to avoid an
  app↔router import cycle. Quiet-hours and known-source constants moved to `config.py`.
- **De-duplication.** The Gmail→Linear path (previously ~50 duplicated lines)
  now has one implementation, `routers/items.capture_to_linear`, called by both
  the generic `/api/items/{source}/{msg_id}/linear` route and the back-compat
  `/api/agents/gmail/items/{msg_id}/linear` route. Gmail archive logic lives once
  in `services/gmail_actions.py`. Dead `score_slacks_concurrent` removed; a
  function-local `import asyncio` hoisted to module scope.

## 4. Performance

- **HTTP client reuse.** `SlackClient`, `OtterClient`, and `FreshserviceClient`
  now lazily create and reuse one `httpx.AsyncClient` across a poll's many calls
  (connection pooling instead of a fresh TCP+TLS handshake per request — the
  Slack poll, with dozens of calls, benefits most). Each fetcher closes its client
  in a `finally`, and the status endpoints do the same.
- Gmail service caching (above) also removes redundant discovery-client builds.

## 5. Security / hygiene

- `/api/llm/status` no longer dumps the full `PATH`/`PATHEXT` environment; it
  returns only the CLI resolution booleans/paths and the model.
- `README.md` rewritten to match the current system (all four sources, Linear,
  wiki, the module map, and the dev/test commands) instead of the stale "Phase 0"
  text.

## Not done (deliberately)

- **Linear HTTP client reuse** — left as one-off `AsyncClient` per call. It's 1–2
  user-initiated calls per capture; reuse there adds shutdown-lifecycle complexity
  for little gain. Noted for later if Linear traffic grows.
- **Synchronous SQLite on the event loop** — unchanged. At single-user volume the
  per-call commit is invisible; revisit (batch writes or `to_thread`) only if a
  large poll starts stalling the heartbeat.

## Verification

- Route table diffed before/after each stage — **identical, 48 routes**.
- `import haven.main` succeeds (transitively loads all routers/services/sources).
- `pytest` — **66 passed**.

*Note: a couple of these checks had to be retried because the dev sandbox's file
mount intermittently served stale/garbled copies of just-written files; the
checks above all passed together in a clean window, and the committed files are
the authoritative ones.*
