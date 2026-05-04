# Haven — Ground Rules

Hard boundaries for any code, agent, or change inside `Projects/Haven/`. These override convenience or feature considerations.

## 1. Never delete data from external services

**Haven must never delete, trash, or destructively modify content in any source system it connects to.** This applies to all current and future agents:

- **Gmail** — never call `messages.delete`, `messages.trash`, or `messages.batchDelete`. **Archive (removing the `INBOX` label via `messages.modify`) is allowed** when triggered by an explicit user click — it is non-destructive (the message stays in All Mail and is fully recoverable). Same goes for read-state changes via `messages.modify`. Never request `gmail.compose` or `https://mail.google.com/` without explicit approval. "Mark replied" / "Mark done" in the UI still updates Haven's local state only — it does not touch Gmail.
- **Slack** — never call `chat.delete` (even on the bot's own messages), `conversations.archive`, or any destructive admin endpoint.
- **JIRA** — never call `DELETE /issue/{key}`. Status transitions to "Done" / "Resolved" are allowed when explicitly triggered by Garth from the UI; outright deletion is not.
- **Linear** — `issueArchive` is **not** the same as delete and is allowed when Garth explicitly archives via the UI. Never call `issueDelete`.
- **Otter.ai** — read-only.
- **Freshservice** — never call `DELETE /tickets/{id}`. Status changes are allowed only on explicit user action.
- **Local filesystem (`data/`)** — append-only. To retire content, mark it deprecated in frontmatter; do not unlink files. Compaction passes (e.g. monthly rollup of old per-email MDs) move content into a rollup file but never destroy the originals.

**Why:** Garth wants Haven to be a layer *over* his real systems, not a replacement that can mutate them. The cost of losing real data dwarfs any speed/cleanliness benefit of deletion.

**How to apply:** Before adding any new agent or action, audit it for delete code paths. Prefer read-only OAuth scopes wherever the source supports them. If a feature seems to require deletion, surface the design choice to Garth before implementing.

## 2. Localhost-only by default

Server binds `127.0.0.1`. Tailscale or remote exposure is opt-in, not opt-out.

## 3. Secrets never leave `.env`

Never write credentials, tokens, or API keys to any markdown file, log line, response body, or chat output. `.env` is the only home for secrets and is gitignored.

## 4. No autonomous outbound communication

Haven drafts replies, alerts, and AR captures, but the actual send/post action requires Garth's explicit click. The Slack DM bot sends digests *to* Garth (no external recipients) and inbound capture creates Linear issues — but Haven never posts in channels, replies on Garth's behalf, or sends email without an explicit user action.
