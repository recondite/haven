# Haven — Code Review

*Reviewed: 2026-05-29. Scope: the `haven/` Python package (~5,000 lines across 19 modules). Focus areas: structure & maintainability, bugs & correctness, security, performance. This is a review only — no code was changed.*

## Summary

Haven is a well-built single-user app. The code is readable, the docstrings are unusually good (they capture *why*, not just *what*), and the design is coherent: a FastAPI orchestrator, four per-source fetchers behind a common `summary()` payload shape, a SQLite dedup/cache store, an SSE event bus, and an LLM scoring layer. The hard parts — Windows `claude` CLI shell-out, Gmail metadata-first filtering, Slack unread-cursor logic — are handled thoughtfully.

The main weaknesses are concentrated and fixable: `main.py` has grown to 1,365 lines and mixes routing, business logic, and a duplicated Gmail/Linear path; there is a real **token-refresh race** and an **encapsulation break** on the SQLite store under concurrent polling; HTTP clients are recreated on every call; and there are **no tests** despite several pure functions that are trivial to cover. None of these block the app from working today, but the structural ones will slow every future change.

Priority ranking of the most important items:

| # | Item | Lens | Severity |
|---|------|------|----------|
| 1 | `main.py` is a 1,365-line monolith; split into routers + a service layer | Structure | High |
| 2 | Gmail token refresh races under parallel fetch (concurrent file writes) | Bug/Correctness | High |
| 3 | `gmail_apply_noise_label` reaches into `cursor_store._conn` outside the lock | Bug/Correctness | High |
| 4 | `item_to_linear` and `gmail_to_linear` are ~50 duplicated lines | Structure | Medium |
| 5 | New `httpx.AsyncClient` per API call — no connection reuse | Performance | Medium |
| 6 | No test coverage on pure logic (`filters`, `enrichment`, `contacts`) | Maintainability | Medium |
| 7 | Synchronous SQLite calls run on the event loop | Performance | Medium |
| 8 | `GmailFetcher._service()` rebuilds the client ~4× per message | Performance | Medium |
| 9 | Repo hygiene: tracked `.pyc`, mixed 3.10/3.14 artifacts, version skew | Structure | Low |
| 10 | `/api/llm/status` leaks full `PATH`; unbounded OAuth `_pending` dict | Security | Low |

---

## Structure & maintainability

**`main.py` (1,365 lines) is doing too much.** It holds every route for six concerns (Gmail, Slack, Freshservice, Otter, wiki, contacts, auth, OAuth, SSE), the quiet-hours scheduler, the heartbeat loop, the favicon, *and* business logic like the entire Gmail poll pipeline (Passes A–E, lines 726–926). This is the single biggest drag on the codebase. FastAPI's `APIRouter` is built for exactly this. A clean split:

```
haven/
  app.py              # FastAPI app, lifespan, scheduler, SSE, health
  routers/
    gmail.py          # /api/agents/gmail/*  + OAuth
    slack.py
    freshservice.py
    otter.py
    wiki.py
    contacts.py
    items.py          # source-generic mark-done/snooze/linear
  services/
    gmail_poll.py     # the Pass A–E pipeline, out of the route handler
```

The Gmail poll pipeline (Passes A–E) is the clearest example of logic that should not live in a route handler — it's ~200 lines of orchestration that you'd want to test independently of HTTP.

**The Gmail→Linear path is duplicated.** `item_to_linear` (lines 443–495) and the back-compat `gmail_to_linear` (lines 499–551) are nearly identical — same cache lookup, same `create_issue_from_email`, same error handling, same payload update, same publish. The back-compat route can delegate to the generic one in two lines instead of copying fifty.

**Scoring parse logic is duplicated.** `score_email` (scoring.py 139–162) and `score_slack` (273–295) share an identical result-coercion block. Factor a `_parse_score(result) -> dict`. Likewise `score_emails_concurrent` and `score_slacks_concurrent` are the same function with a different callee — one generic `_score_concurrent(items, scorer, max_concurrent)` covers both. (Note `score_slacks_concurrent` appears unused — `slack_poll` does its own gather — so it may be dead code.)

**Token-refresh logic exists in two places.** `_gmail_service()` in main.py (220–229) and `GmailFetcher._service()` in gmail.py (172–179) implement the same refresh-and-persist dance. One shared helper on `GmailAuth` (e.g. `GmailAuth.service()`) removes the drift risk and is the natural home for the race fix below.

**Minor structure notes.** `import asyncio` appears inside functions in scoring.py (170, 302) — hoist to module top. `import os as _os` / `from haven import llm` inside `llm_status` (174–176) and several `from ... import` inside route bodies are deferred imports that aren't needed. `_strip_html` is defined in both gmail.py (32) and freshservice.py (358) — consolidate into one util module.

---

## Bugs & correctness

**Gmail token refresh races under parallel polling (High).** During a poll, `gmail_poll` fans out to concurrency 10 (metadata) and 5 (full fetch + thread state). Each call hits `GmailFetcher._service()` (gmail.py 172), and on an expired token *every concurrent caller* independently runs `creds.refresh(Request())` and then `self.auth.token_path.write_text(creds.to_json())` (178). Concurrent writers to the same `gmail-token.json` can interleave and corrupt the file, and you'll burn N refreshes instead of one. The same unguarded refresh-and-write also lives in `_gmail_service()` (main.py 226–228). Fix: refresh once behind an `asyncio.Lock` (or refresh eagerly before fan-out), and write the token atomically (write to a temp file, then `os.replace`).

**`cursor_store._conn` is accessed outside the lock (High).** `gmail_apply_noise_label` runs `cursor_store._conn.execute("SELECT ... FROM seen_items ...")` directly (main.py 661–663), bypassing both the public API and `self._lock`. Every other DB access goes through a locked method. Sharing one `sqlite3` connection (`check_same_thread=False`) across threads while one caller reads off `_conn` unsynchronized invites "recursive use of cursors"/threading errors if a write lands concurrently. Add a `CursorStore.list_rejected_ids(source)` method and call that instead.

**Otter `force` ignores rejections cleanup symmetry — minor.** In `freshservice_poll`, stale eviction (lines 1153–1157) runs *before* the `force` branch that then wipes the whole cache (1159–1161), so on force you compute and delete `stale_ids` and then immediately clear everything — harmless but redundant work. Reorder so `force` short-circuits the stale computation.

**`auto_approve_from_history` runs on metadata that lacks thread state.** In Pass B, `filters.auto_approve_from_history(meta)` (main.py 797) checks `payload.get("last_outbound_at")`, but `meta` comes from `fetch_metadata` (gmail.py 327–370), which never populates `last_outbound_at` — that field is only filled later during full-fetch enrichment (`_enrich`, gmail.py 398–402). So this auto-approve branch is effectively always `False` at the point it's called. Either fetch thread state in the metadata pass for borderline items, or move this check to Pass E where the data exists. Worth confirming against intent — the comment promises behavior the data flow can't deliver yet.

**`OtterItem.summary()` sets both `snippet` and `summary` to the full AR text** (otter.py 90, 107) while `subject` is truncated to 110 chars. Not a bug per se, but `summary` is documented elsewhere as "<=120 chars"; downstream consumers (e.g. Linear description) may get an unexpectedly long string. Confirm the UI/Linear truncation handles it.

**Silent event drops.** `EventBus.publish` drops events when a subscriber queue is full (events.py 23–25, maxsize 200). That's a deliberate "don't block producers" choice and fine for a live dashboard, but if an SSE client stalls it will silently miss state changes (e.g. a `*_handled` event) and drift from server truth until the next full poll/refetch. Acceptable given the design; flag it so it's a known limitation, not a surprise.

**Unbounded OAuth `_pending` dict.** `GmailAuth._pending` (gmail_auth.py 34) accumulates `state -> code_verifier` on every `begin()` and only pops on a successful `complete()`. Abandoned auth attempts leak entries forever. Trivial for a single user, but worth a TTL or size cap if this ever goes multi-user.

---

## Security

The fundamentals are right: `.env`, `data/`, and `secrets/` are all git-ignored and **not** tracked (verified). Gmail uses `gmail.modify` scope (no delete), archive is label-removal only per the ground rules, and `OAUTHLIB_INSECURE_TRANSPORT`/`RELAX_TOKEN_SCOPE` are scoped to the localhost OAuth flow with explanatory comments. The app binds to `127.0.0.1` by default. For a single-user local tool this is a reasonable posture.

Smaller items:

- **`/api/llm/status` discloses the full environment `PATH`** (first 3,000 chars) and `PATHEXT` (main.py 188–189). Local-only, but there's no reason to expose it — trim to a boolean "claude found" plus the resolved path. The `/api/llm/test` endpoint also accepts `GET` and shell-executes the CLI; harmless locally but a surprising verb for a side-effecting probe.
- **No auth on any endpoint.** Fine while bound to loopback, but note that anyone able to reach the port can trigger Gmail archiving, Linear issue creation, and wiki writes. If the host is ever bound to `0.0.0.0` or port-forwarded, this becomes serious. Consider a simple shared-secret header check as cheap insurance.
- **Wiki path safety looks sound.** `is_safe_path` (wiki.py 64–82) rejects `..`, leading slashes, and anything outside the allow-listed folders, and protects `SCHEMA.md`/`log.md`. Good. One belt-and-braces addition: also `os.path.normpath` + confirm the resolved path stays under `WIKI_DIR` before writing, in case of regex edge cases.
- **LLM prompt injection into the wiki.** `ingest_source` feeds untrusted email bodies to the model and writes whatever files the model returns (within the path allow-list). A crafted email could try to steer the model into rewriting existing pages. The path allow-list limits blast radius to the wiki itself; the "preserve prior content" instruction is a prompt-level mitigation, not a guarantee. Acceptable for now given it's a manual, single-user ingest — just a known risk.

---

## Performance

**A new `httpx.AsyncClient` per API call.** Slack (`_call`, slack.py 138), Otter (134), Freshservice (185), and Linear (45) all open and tear down a client — and therefore a fresh TCP+TLS connection — on every single request. Slack is the worst case: `fetch_all` makes dozens of calls per poll (DM list, per-channel history, searches), each paying full handshake cost. Hold one `AsyncClient` per fetcher instance (or per client object) with connection pooling and reuse it; this is likely the biggest single latency win available.

**`GmailFetcher._service()` rebuilds the API client repeatedly.** `build("gmail", "v1", ...)` is called fresh on every `_service()` invocation, and a single `fetch_message` triggers it ~4× (fetch + `user_email` + `labels_map` + `_fetch_thread_state`). Across a poll of N survivors at concurrency 5, that's a lot of redundant client construction. Cache the built service on the fetcher (invalidate only on token refresh).

**Synchronous SQLite on the event loop.** All `cursor_store` methods are synchronous and are called directly from `async` handlers (not via `asyncio.to_thread`). Each `put_cached`/`list_cached` blocks the loop while it commits. At single-user volume this is invisible, but during a large poll that writes many payloads it can add up and stalls the heartbeat/SSE. Options: batch the writes, or move DB work to a thread (`to_thread`) given the connection is already `check_same_thread=False`.

**Per-item LLM commits.** `slack_poll`'s `_score_one` does `put_cached` + `mark_seen` (two commits) per item as scores stream in (main.py 1073–1074). That's intentional for progressive UI streaming and is the right call — just noting it's a deliberate latency-for-responsiveness trade.

**`derive_contacts` recomputed per request** over all cached items across four sources (contacts.py / main.py 1299). Documented as cheap and it is at current scale; if cache sizes grow, a short TTL memo keyed on cache mutation count would help.

---

## Tests & tooling

**There are no tests.** For an app this size that's the most consequential maintainability gap, and the irony is that the highest-value targets are the *easiest* to test — they're pure functions:

- `filters.apply_filter` / `watchlist_match` / `is_blocked` — pure, rule-heavy, exactly where regressions hurt. Table-driven tests would pay for themselves immediately.
- `enrichment.company_from_domain` / `garth_recipient_role` / `derive_thread_state` / `dates_mentioned` — pure, deterministic.
- `contacts.derive_contacts` — pure, takes a list of dicts.
- `llm._extract_json` — pure parser with several branches.
- `wiki.is_safe_path` — security-relevant, must not regress.

`pyproject.toml` already implies a tooling intent (`.pytest_cache`, `.ruff_cache`, `.mypy_cache` are git-ignored) but none are configured. Adding `pytest` + `ruff` + a handful of tests on the functions above would be a high-leverage afternoon.

**Repo hygiene.** `haven/__pycache__` and `haven/sources/__pycache__` contain tracked-looking `.pyc` files spanning **cpython-310 and cpython-314**, while `pyproject.toml` pins `requires-python = ">=3.12"`. The mixed bytecode suggests the project has been run under at least three interpreters. Confirm `.pyc` aren't committed (the `.gitignore` covers them, so likely just local cruft) and pick one supported interpreter. The `README` quickstart is also stale relative to the current feature set (it describes "Phase 0… counter ticking every 2s" and "Up next: Phase 1," but the code is through Phase 2.2).

---

## Suggested sequence (when you're ready to act)

1. **Fix the two correctness issues first** (token-refresh race; `_conn` lock bypass) — small, isolated, and they protect data integrity.
2. **Add a test harness** (`pytest` + `ruff`) and cover the pure functions — this is your safety net for everything after.
3. **Extract the Gmail poll pipeline** out of the route handler into `services/gmail_poll.py`, now that tests can catch regressions.
4. **Split `main.py` into routers**, de-duplicating the Linear path as you go.
5. **Reuse HTTP clients** and cache the Gmail service — the performance wins.
6. **Repo cleanup** — interpreter pin, README refresh, trim the `PATH`-leaking status endpoint.

Everything here is incremental; none of it requires a rewrite. The bones are good.
