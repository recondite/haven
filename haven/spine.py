"""Domain spine — the normalized store every component will eventually read/write
through. Phase 0 slice: only the `item` table exists, populated by dual-write from
the existing `cached_items` cache (see db.put_cached). Reads stay on cached_items
until a week's diff is clean, then flip (plan v4 §5 migration mitigation).

ponytail: single module + PRAGMA user_version migrations, not a package + a
migration framework. Add tables (person, request, job, draft, action, audit,
feedback) in the phase that first writes them — one CREATE TABLE each. Promote to
a package when it grows a second concern.
"""
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
]

_KIND_BY_SOURCE = {
    "gmail": "message",
    "slack": "message",
    "freshservice": "ticket",
    "otter": "ar",
    "jira": "issue",
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
        "sender": payload.get("sender") or payload.get("from"),
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


spine = Spine(config.DATA_DIR / "state" / "spine.sqlite")
