# Haven

Real-time "action required" dashboard for Garth (CIO, Ayar Labs). A FastAPI
orchestrator polls several sources, filters + LLM-scores each item, and streams
the survivors to a single-page UI over SSE.

## Quickstart

**One-time setup**

```sh
winget install astral-sh.uv
```

Restart the terminal so `uv` is on PATH, then copy `.env.example` to `.env` and
fill in the tokens you have (each source degrades gracefully if its token is
missing).

**Run**

```sh
cd Projects/Haven
uv sync
uv run python -m haven
```

Open http://127.0.0.1:8765/. Connect Gmail via the in-app OAuth link
(`/oauth/authorize`).

## Sources

| Source       | Auth                         | Scoring                          |
|--------------|------------------------------|----------------------------------|
| Gmail        | Google OAuth (`gmail.modify`)| pre-filter rules + LLM (Haiku)   |
| Slack        | user + bot tokens            | LLM for channels, deterministic DMs |
| Freshservice | API key + domain             | deterministic (priority → urgency) |
| Otter.ai     | API key                      | deterministic (every AR is an action) |

Items can be marked done, snoozed, captured to **Linear**, or ingested into the
LLM-maintained **wiki** under `data/wiki/`.

## Architecture

```
haven/
  main.py              # app assembly: lifespan + scheduler, heartbeat, SSE, health, llm status
  deps.py              # shared singletons (gmail_auth)
  config.py            # .env-backed settings + tunables (quiet hours, known sources)
  db.py                # SQLite dedup / rejection / cached-payload store
  events.py            # in-memory pub/sub bus -> SSE
  filters.py           # deterministic pre-LLM keep/reject rules + block/watch lists
  scoring.py           # LLM scoring (Gmail + Slack)
  enrichment.py        # deterministic enrichment (company, thread state, dates)
  contacts.py          # cross-source contact derivation
  linear.py            # Linear GraphQL client (non-destructive issue create)
  wiki.py              # LLM-maintained curated wiki
  llm.py               # claude CLI shell-out
  routers/             # one APIRouter per concern (gmail, slack, freshservice,
                       #   otter, wiki, contacts, items)
  services/
    gmail_poll.py      # the Gmail poll pipeline (Pass A–E), out of the route handler
    gmail_actions.py   # shared Gmail write actions (archive = label removal)
  sources/             # per-source fetchers (gmail, slack, freshservice, otter)
```

Polling is scheduled per source (intervals from `agents/*.yaml`, `enabled: false`
to disable) and honors quiet hours. All write actions are non-destructive per the
ground rules in `ground_rules/` (Gmail archive = INBOX-label removal, never delete;
no Linear deletes).

## Development

```sh
uv sync --extra dev
uv run pytest          # unit tests for the pure logic (filters, enrichment, contacts, wiki, linear, llm)
uv run ruff check .    # lint
```
