# Haven

Real-time AR dashboard. Plan: `~/.claude/plans/give-ma-plan-to-replicated-haven.md`.

## Phase 0 quickstart

**One-time setup**

```sh
winget install astral-sh.uv
```

Restart the terminal so `uv` is on PATH.

**Run**

```sh
cd Projects/Haven
uv sync
uv run python -m haven
```

Open http://127.0.0.1:8765/ — you should see a counter ticking every 2s.

## What's wired

- ✅ FastAPI on `127.0.0.1:8765`, SSE heartbeat at `/api/sse/stream`
- ✅ `.env` with all six tokens captured (Slack ×4, Freshservice, Google OAuth)
- ✅ Slack app installed with bot+user scope split

## Up next

- Phase 1: Gmail vertical slice (OAuth, fetcher, enrichment, Reply Needed panel)
