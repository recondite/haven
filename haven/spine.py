"""Domain spine — the normalized store every component will eventually read/write
through. Phase 0 slice: only the `item` table exists, populated by dual-write from
the existing `cached_items` cache (see db.put_cached). Reads stay on cached_items
until a week's diff is clean, then flip (plan v4 §5 migration mitigation).

ponytail: single module + PRAGMA user_version migrations, not a package + a
migration framework. Add tables (person, request, job, draft, action, audit,
feedback) in the phase that first writes them — one CREATE TABLE each. Promote to
a package when it grows a second concern.
"""
import json
import sqlite3
import threading
from pathlib import Path

from haven import config

# Forward-only migrations. Index in this list == the schema version it produces.
# Never edit a shipped entry; append a new one.
_MIGRATIONS: list[str] = [
    # v1 — the item table (normalized; dedup/payload still live in cached_items).
    """
    CREATE TABLE item (
        source       TEXT NOT NULL,
        external_id  TEXT NOT NULL,
        kind         TEXT,               -- message | ticket | issue | ar
        subject      TEXT,
        sender       TEXT,
        url          TEXT,
        status       TEXT,               -- open | handled | snoozed
        score        REAL,
        tags         TEXT,
        thread_id    TEXT,
        handled_at   REAL,
        snooze_until REAL,
        first_seen   TEXT NOT NULL DEFAULT (datetime('now')),
        last_seen    TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (source, external_id)
    );
    """,
    # v2 — Phase 1 dispatch pipeline: job -> draft -> (approve) -> action, with
    # job_step tracing, append-only audit, and feedback captured at the gate.
    # Invariants: action.draft_id is UNIQUE (one approval = exactly one action);
    # action.draft_id FK -> draft (no action without a real draft). Only the
    # executor inserts action rows.
    """
    CREATE TABLE job (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        agent       TEXT NOT NULL,
        runtime     TEXT,
        subject_ref TEXT,               -- e.g. "slack/C123:1699..."
        status      TEXT NOT NULL,      -- running | done | failed | dead_letter
        tokens      INTEGER,
        cost_usd    REAL,
        exit_reason TEXT,
        retries     INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE job_step (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id     INTEGER NOT NULL REFERENCES job(id),
        seq        INTEGER NOT NULL,
        tool       TEXT NOT NULL,
        detail     TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE draft (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id     INTEGER REFERENCES job(id),
        kind       TEXT NOT NULL,       -- email | slack | task | wiki
        target     TEXT,                -- where it would go
        payload    TEXT NOT NULL,       -- the draft content
        evidence   TEXT,                -- json: cited sources
        status     TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | edited
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE action (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        draft_id   INTEGER NOT NULL UNIQUE REFERENCES draft(id),
        kind       TEXT NOT NULL,
        target     TEXT,
        status     TEXT NOT NULL,       -- dry_run | sent | failed
        result     TEXT,                -- json
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE audit (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        actor      TEXT NOT NULL,       -- system | gt | agent
        action     TEXT NOT NULL,
        entity     TEXT,
        entity_id  INTEGER,
        detail     TEXT,
        ts         TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE feedback (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        draft_id      INTEGER NOT NULL REFERENCES draft(id),
        verdict       TEXT NOT NULL,    -- approved_clean | edited | rejected
        edit_distance INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    # v3 — draft editing: keep the agent's original text so the approval gate can
    # record an honest edited-vs-clean verdict + edit distance (feedback signal).
    """
    ALTER TABLE draft ADD COLUMN original_payload TEXT;
    """,
    # v4 — identity: roster from SecondBrain (person) + system-id resolution
    # (identity_map). Manual overrides win permanently; unresolved senders are
    # derived by query, never silently guessed.
    """
    CREATE TABLE person (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        secondbrain_page TEXT UNIQUE,      -- people/<slug>.md
        name             TEXT NOT NULL,
        title            TEXT,
        department       TEXT,
        manager          TEXT,
        work_email       TEXT,
        is_report        INTEGER NOT NULL DEFAULT 0,  -- direct report of GT
        updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE UNIQUE INDEX idx_person_email ON person(work_email) WHERE work_email IS NOT NULL;
    CREATE TABLE identity_map (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        person_id          INTEGER NOT NULL REFERENCES person(id),
        system             TEXT NOT NULL,   -- slack | jira | freshservice | gmail | otter
        system_id          TEXT NOT NULL,
        confidence         REAL NOT NULL DEFAULT 1.0,
        provenance         TEXT,            -- email_match | manual | ...
        is_manual_override INTEGER NOT NULL DEFAULT 0,
        resolved_at        TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(system, system_id)
    );
    """,
    # v5 — M0 safety enforcement (build plan v2): audit becomes append-only by
    # TRIGGER, not docstring — any UPDATE/DELETE aborts, bug or not. runtime_config
    # holds operator-facing switches (send_mode panic switch / boot tripwire);
    # consulted by executor.is_dry_run() ahead of the env var.
    """
    CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
    BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
    CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
    BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
    CREATE TABLE runtime_config (
        key        TEXT PRIMARY KEY,
        value      TEXT,
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_by TEXT
    );
    """,
    # v6 — person pages: Garth-authored notes pinned to a person. Append-only,
    # hide-not-delete (per ground rule #1 on local data). Everything else the
    # dossier shows is aggregated on-demand from caches + SecondBrain, not stored.
    """
    CREATE TABLE person_note (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        person_id  INTEGER NOT NULL REFERENCES person(id),
        body       TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        hidden_at  TEXT
    );
    CREATE INDEX idx_person_note_person ON person_note(person_id);
    """,
]

_KIND_BY_SOURCE = {
    "gmail": "message",
    "slack": "message",
    "freshservice": "ticket",
    "otter": "ar",
    "jira": "issue",
    "asana": "task",
}

# Fields the item row projects from the cached payload; used for both upsert and diff.
_FIELDS = ("kind", "subject", "sender", "url", "status", "score", "tags", "thread_id",
           "handled_at", "snooze_until")


def _project(source: str, payload: dict) -> dict:
    """Payload dict -> item-row fields. The single source of truth for the mapping."""
    handled = payload.get("handled_at")
    snooze = payload.get("snooze_until")
    status = "handled" if handled else ("snoozed" if snooze else "open")
    return {
        "kind": _KIND_BY_SOURCE.get(source),
        "subject": payload.get("subject"),
        "sender": payload.get("sender_email") or payload.get("sender") or payload.get("from"),
        "url": payload.get("url"),
        "status": status,
        "score": payload.get("score"),
        "tags": payload.get("tag"),
        "thread_id": payload.get("thread_id"),
        "handled_at": handled,
        "snooze_until": snooze,
    }


class Spine:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")  # enforce draft/action integrity
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            v = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for i in range(v, len(_MIGRATIONS)):
                self._conn.executescript(_MIGRATIONS[i])
                self._conn.execute(f"PRAGMA user_version = {i + 1}")
            self._conn.commit()

    def upsert_item(self, source: str, external_id: str, payload: dict) -> None:
        """Dual-write hook: mirror a cached payload into the item table.
        Preserves first_seen, bumps last_seen. Called from db.put_cached."""
        f = _project(source, payload)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO item (source, external_id, kind, subject, sender, url,
                                  status, score, tags, thread_id, handled_at, snooze_until)
                VALUES (:source, :external_id, :kind, :subject, :sender, :url,
                        :status, :score, :tags, :thread_id, :handled_at, :snooze_until)
                ON CONFLICT(source, external_id) DO UPDATE SET
                    kind=excluded.kind, subject=excluded.subject, sender=excluded.sender,
                    url=excluded.url, status=excluded.status, score=excluded.score,
                    tags=excluded.tags, thread_id=excluded.thread_id,
                    handled_at=excluded.handled_at, snooze_until=excluded.snooze_until,
                    last_seen=datetime('now')
                """,
                {"source": source, "external_id": external_id, **f},
            )
            self._conn.commit()

    def get_item(self, source: str, external_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM item WHERE source = ? AND external_id = ?",
                (source, external_id),
            ).fetchone()
            return dict(row) if row else None

    def diff_source(self, cached_payloads: dict[str, dict], source: str) -> list[dict]:
        """Validation for the dual-write window: compare cached payloads against
        item rows. Returns one entry per mismatch — empty list means parity.

        cached_payloads: {external_id: payload} from cursor_store.list_cached.
        """
        with self._lock:
            rows = {
                r["external_id"]: dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM item WHERE source = ?", (source,)
                ).fetchall()
            }
        out: list[dict] = []
        for ext_id, payload in cached_payloads.items():
            row = rows.get(ext_id)
            if row is None:
                out.append({"external_id": ext_id, "reason": "missing_in_item"})
                continue
            expected = _project(source, payload)
            bad = {k: {"item": row.get(k), "cached": expected[k]}
                   for k in _FIELDS if row.get(k) != expected[k]}
            if bad:
                out.append({"external_id": ext_id, "reason": "field_mismatch", "fields": bad})
        return out

    # ─── Phase 1 dispatch lifecycle ──────────────────────
    def audit(self, actor: str, action: str, entity: str | None = None,
              entity_id: int | None = None, detail: dict | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit (actor, action, entity, entity_id, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (actor, action, entity, entity_id, json.dumps(detail) if detail else None),
            )
            self._conn.commit()

    def create_job(self, agent: str, runtime: str, subject_ref: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO job (agent, runtime, subject_ref, status) VALUES (?, ?, ?, 'running')",
                (agent, runtime, subject_ref),
            )
            self._conn.commit()
            job_id = int(cur.lastrowid)
        self.audit("agent", "job_started", "job", job_id, {"agent": agent, "subject": subject_ref})
        return job_id

    def log_step(self, job_id: int, seq: int, tool: str, detail: dict | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO job_step (job_id, seq, tool, detail) VALUES (?, ?, ?, ?)",
                (job_id, seq, tool, json.dumps(detail) if detail else None),
            )
            self._conn.commit()

    def finish_job(self, job_id: int, status: str, exit_reason: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE job SET status=?, exit_reason=?, updated_at=datetime('now') WHERE id=?",
                (status, exit_reason, job_id),
            )
            self._conn.commit()

    def create_draft(self, job_id: int | None, kind: str, target: str,
                     payload: str, evidence: list | dict | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO draft (job_id, kind, target, payload, evidence) VALUES (?, ?, ?, ?, ?)",
                (job_id, kind, target, payload, json.dumps(evidence) if evidence else None),
            )
            self._conn.commit()
            draft_id = int(cur.lastrowid)
        self.audit("agent", "draft_created", "draft", draft_id, {"kind": kind, "target": target})
        return draft_id

    def get_draft(self, draft_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()
            return dict(row) if row else None

    def list_drafts(self, status: str = "pending") -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM draft WHERE status=? ORDER BY created_at DESC", (status,)
            ).fetchall()
            return [dict(r) for r in rows]

    def edit_draft(self, draft_id: int, new_payload: str) -> None:
        """Replace a pending draft's payload, preserving the agent's original
        (first edit wins the snapshot) so approve can score the edit."""
        with self._lock:
            self._conn.execute(
                "UPDATE draft SET original_payload = COALESCE(original_payload, payload), "
                "payload = ?, updated_at = datetime('now') WHERE id = ?",
                (new_payload, draft_id),
            )
            self._conn.commit()
        self.audit("gt", "draft_edited", "draft", draft_id, {"chars": len(new_payload)})

    def set_draft_status(self, draft_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE draft SET status=?, updated_at=datetime('now') WHERE id=?",
                (status, draft_id),
            )
            self._conn.commit()

    def record_action(self, draft_id: int, kind: str, target: str, status: str,
                      result: dict | None = None) -> tuple[int, bool]:
        """Insert the single action for a draft. Idempotent: the UNIQUE(draft_id)
        constraint means a double-approve or a restart mid-approve yields exactly
        one row. Returns (action_id, created) — created=False if it already existed.
        ONLY the executor should call this."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO action (draft_id, kind, target, status, result) "
                "VALUES (?, ?, ?, ?, ?)",
                (draft_id, kind, target, status, json.dumps(result) if result else None),
            )
            self._conn.commit()
            if cur.rowcount == 1:
                return int(cur.lastrowid), True
            row = self._conn.execute("SELECT id FROM action WHERE draft_id=?", (draft_id,)).fetchone()
            return int(row["id"]), False

    def record_feedback(self, draft_id: int, verdict: str, edit_distance: int = 0) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO feedback (draft_id, verdict, edit_distance) VALUES (?, ?, ?)",
                (draft_id, verdict, edit_distance),
            )
            self._conn.commit()

    def update_action(self, action_id: int, status: str, result: dict | None = None) -> None:
        """Transition an action's send state (sending -> sent | failed | unverified).
        Append-only spirit: rows are never deleted; only their status advances."""
        with self._lock:
            self._conn.execute(
                "UPDATE action SET status = ?, result = ? WHERE id = ?",
                (status, json.dumps(result) if result else None, action_id),
            )
            self._conn.commit()

    def action_verb_counts(self, days: int = 30) -> dict:
        """Executor outbound-verb usage over the window: count + last-used per
        kind (M4 trust panel). These are the highest-risk write paths — the
        ones the executor actually performs."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) n, MAX(created_at) last, "
                "  SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) sent, "
                "  SUM(CASE WHEN status='dry_run' THEN 1 ELSE 0 END) dry "
                "FROM action WHERE created_at >= datetime('now', ?) GROUP BY kind",
                (f"-{int(days)} days",),
            ).fetchall()
            return {r["kind"]: {"count": r["n"], "last_used": r["last"],
                               "sent": r["sent"], "dry_run": r["dry"]} for r in rows}

    def feedback_by_agent(self) -> list[dict]:
        """approved_clean / edited / rejected counts per dispatch agent (M4)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT j.agent, f.verdict, COUNT(*) n "
                "FROM feedback f JOIN draft d ON d.id=f.draft_id "
                "JOIN job j ON j.id=d.job_id GROUP BY j.agent, f.verdict"
            ).fetchall()
        agg: dict[str, dict] = {}
        for r in rows:
            a = agg.setdefault(r["agent"], {"agent": r["agent"], "approved_clean": 0,
                                            "edited": 0, "rejected": 0})
            a[r["verdict"]] = r["n"]
        return list(agg.values())

    def edited_drafts(self) -> list[dict]:
        """Drafts GT edited before approving (original_payload vs payload) — the
        raw signal for style distillation (M1)."""
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT d.id, d.kind, d.original_payload, d.payload, f.edit_distance "
                "FROM draft d JOIN feedback f ON f.draft_id = d.id "
                "WHERE f.verdict = 'edited' AND d.original_payload IS NOT NULL "
                "ORDER BY d.id DESC").fetchall()]

    def get_action_for_draft(self, draft_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM action WHERE draft_id=?", (draft_id,)).fetchone()
            return dict(row) if row else None

    # ─── Runtime config (operator switches; M0) ──────────
    def get_runtime_config(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM runtime_config WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def set_runtime_config(self, key: str, value: str | None, by: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runtime_config (key, value, updated_by) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_by=excluded.updated_by, updated_at=datetime('now')",
                (key, value, by),
            )
            self._conn.commit()
        self.audit(by, "config_changed", "runtime_config", None, {"key": key, "value": value})

    def list_actions(self, status: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT a.*, d.kind AS draft_kind, d.target AS draft_target, "
                "d.payload AS draft_payload FROM action a JOIN draft d ON d.id=a.draft_id "
                "WHERE a.status=? ORDER BY a.created_at", (status,)).fetchall()]

    def resolve_action(self, action_id: int, status: str, note: str, actor: str = "gt") -> dict:
        """Resolve a stuck send (crash between send and status update). Legal ONLY
        from 'sending' and only to sent|failed — GT verified externally what
        actually happened; the note records the evidence."""
        if status not in ("sent", "failed"):
            raise ValueError(f"resolve status must be sent|failed, got {status!r}")
        with self._lock:
            row = self._conn.execute("SELECT status FROM action WHERE id=?", (action_id,)).fetchone()
            if row is None:
                raise ValueError(f"action {action_id} not found")
            if row["status"] != "sending":
                raise ValueError(f"action {action_id} is {row['status']!r}; only 'sending' can be resolved")
            self._conn.execute(
                "UPDATE action SET status=?, result=? WHERE id=?",
                (status, json.dumps({"resolved_by": actor, "note": note[:500]}), action_id),
            )
            self._conn.commit()
        self.audit(actor, "action_resolved", "action", action_id, {"status": status, "note": note[:300]})
        return {"action_id": action_id, "status": status}

    # ─── Identity ────────────────────────────────────────
    def upsert_person(self, page: str, name: str, title: str | None, department: str | None,
                      manager: str | None, work_email: str | None, is_report: bool = False) -> int:
        with self._lock:
            self._conn.execute(
                """INSERT INTO person (secondbrain_page, name, title, department, manager, work_email, is_report)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(secondbrain_page) DO UPDATE SET
                     name=excluded.name, title=excluded.title, department=excluded.department,
                     manager=excluded.manager, work_email=excluded.work_email,
                     is_report=excluded.is_report, updated_at=datetime('now')""",
                (page, name, title, department, manager, work_email, 1 if is_report else 0),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT id FROM person WHERE secondbrain_page=?", (page,)).fetchone()
            return int(row["id"])

    def list_people(self, reports_only: bool = False) -> list[dict]:
        with self._lock:
            q = "SELECT * FROM person"
            if reports_only:
                q += " WHERE is_report=1"
            q += " ORDER BY name"
            return [dict(r) for r in self._conn.execute(q).fetchall()]

    def person_by_email(self, email: str) -> dict | None:
        if not email:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM person WHERE lower(work_email)=lower(?)", (email,)
            ).fetchone()
            return dict(row) if row else None

    def map_identity(self, person_id: int, system: str, system_id: str,
                     confidence: float = 1.0, provenance: str = "email_match",
                     manual: bool = False) -> None:
        """Record a system id for a person. A manual override is never clobbered
        by an automated resolution."""
        with self._lock:
            existing = self._conn.execute(
                "SELECT is_manual_override FROM identity_map WHERE system=? AND system_id=?",
                (system, system_id),
            ).fetchone()
            if existing and existing["is_manual_override"] and not manual:
                return  # manual wins permanently
            self._conn.execute(
                """INSERT INTO identity_map (person_id, system, system_id, confidence, provenance, is_manual_override)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(system, system_id) DO UPDATE SET
                     person_id=excluded.person_id, confidence=excluded.confidence,
                     provenance=excluded.provenance, is_manual_override=excluded.is_manual_override,
                     resolved_at=datetime('now')""",
                (person_id, system, system_id, confidence, provenance, 1 if manual else 0),
            )
            self._conn.commit()

    def identities_for_person(self, person_id: int) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT system, system_id, confidence, provenance, is_manual_override "
                "FROM identity_map WHERE person_id=? ORDER BY system", (person_id,)
            ).fetchall()]

    # ─── Person notes (v6) ───────────────────────────────
    def add_note(self, person_id: int, body: str) -> dict:
        body = (body or "").strip()
        if not body:
            raise ValueError("note body is empty")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO person_note (person_id, body) VALUES (?, ?)", (person_id, body)
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id, person_id, body, created_at FROM person_note WHERE id=?",
                (cur.lastrowid,),
            ).fetchone()
            return dict(row)

    def list_notes(self, person_id: int) -> list[dict]:
        """Active (non-hidden) notes, newest first."""
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT id, person_id, body, created_at FROM person_note "
                "WHERE person_id=? AND hidden_at IS NULL ORDER BY created_at DESC, id DESC",
                (person_id,),
            ).fetchall()]

    def hide_note(self, note_id: int) -> bool:
        """Soft-delete: mark hidden, never DELETE (append-only local data)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE person_note SET hidden_at=datetime('now') "
                "WHERE id=? AND hidden_at IS NULL",
                (note_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def identity_coverage(self) -> dict:
        with self._lock:
            people = self._conn.execute("SELECT COUNT(*) FROM person").fetchone()[0]
            mapped = self._conn.execute(
                "SELECT COUNT(DISTINCT person_id) FROM identity_map").fetchone()[0]
            by_system = {r["system"]: r["n"] for r in self._conn.execute(
                "SELECT system, COUNT(*) n FROM identity_map GROUP BY system").fetchall()}
            return {"people": people, "people_with_any_id": mapped, "by_system": by_system}


spine = Spine(config.DATA_DIR / "state" / "spine.sqlite")
