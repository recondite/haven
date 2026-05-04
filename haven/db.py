"""SQLite-backed cursor + dedup + cached-payload store. Shared across agents."""
import json
import sqlite3
import threading
from pathlib import Path

from haven import config


class CursorStore:
    """Tiny SQLite store for per-source dedup and poll cursors.

    Schema:
      seen_items(source, item_id, fetched_at) — items already processed
      cursors(source, key, value) — per-source state (e.g. last_history_id)
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS seen_items (
                    source TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (source, item_id)
                );
                CREATE TABLE IF NOT EXISTS cursors (
                    source TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT,
                    PRIMARY KEY (source, key)
                );
                CREATE TABLE IF NOT EXISTS cached_items (
                    source TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (source, item_id)
                );
                """
            )
            # Lightweight migration: add status/reject_reason columns to seen_items
            # for tracking pre-filter rejections. Idempotent — ignores "duplicate column" errors.
            for col_def in (
                "ADD COLUMN status TEXT",
                "ADD COLUMN reject_reason TEXT",
            ):
                try:
                    self._conn.execute(f"ALTER TABLE seen_items {col_def}")
                except sqlite3.OperationalError:
                    pass  # column already exists
            self._conn.commit()

    def is_seen(self, source: str, item_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM seen_items WHERE source = ? AND item_id = ?",
                (source, item_id),
            )
            return cur.fetchone() is not None

    def mark_seen(self, source: str, item_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO seen_items (source, item_id) VALUES (?, ?)",
                (source, item_id),
            )
            self._conn.commit()

    def seen_count(self, source: str) -> int:
        """Count of items processed (excludes pre-filter rejections)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM seen_items "
                "WHERE source = ? AND (status IS NULL OR status != 'rejected')",
                (source,),
            )
            return int(cur.fetchone()[0])

    def rejected_count(self, source: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM seen_items WHERE source = ? AND status = 'rejected'",
                (source,),
            )
            return int(cur.fetchone()[0])

    def is_rejected(self, source: str, item_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT status FROM seen_items WHERE source = ? AND item_id = ?",
                (source, item_id),
            )
            row = cur.fetchone()
            return row is not None and row[0] == "rejected"

    def get_rejected_set(self, source: str, item_ids: list[str]) -> set[str]:
        if not item_ids:
            return set()
        with self._lock:
            placeholders = ",".join("?" * len(item_ids))
            cur = self._conn.execute(
                f"SELECT item_id FROM seen_items "
                f"WHERE source = ? AND status = 'rejected' AND item_id IN ({placeholders})",
                (source, *item_ids),
            )
            return {row[0] for row in cur.fetchall()}

    def mark_rejected(self, source: str, item_id: str, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO seen_items (source, item_id, status, reject_reason) "
                "VALUES (?, ?, 'rejected', ?) "
                "ON CONFLICT(source, item_id) DO UPDATE SET "
                "  status = 'rejected', reject_reason = excluded.reject_reason",
                (source, item_id, reason),
            )
            self._conn.commit()

    def clear_rejections(self, source: str, item_ids: list[str] | None = None) -> int:
        """Clear rejection markers so the next poll re-evaluates these items."""
        with self._lock:
            if item_ids is None:
                cur = self._conn.execute(
                    "DELETE FROM seen_items WHERE source = ? AND status = 'rejected'",
                    (source,),
                )
            elif not item_ids:
                return 0
            else:
                placeholders = ",".join("?" * len(item_ids))
                cur = self._conn.execute(
                    f"DELETE FROM seen_items "
                    f"WHERE source = ? AND status = 'rejected' AND item_id IN ({placeholders})",
                    (source, *item_ids),
                )
            self._conn.commit()
            return cur.rowcount

    def get_cursor(self, source: str, key: str) -> str | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT value FROM cursors WHERE source = ? AND key = ?",
                (source, key),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def set_cursor(self, source: str, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO cursors (source, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(source, key) DO UPDATE SET value = excluded.value",
                (source, key, value),
            )
            self._conn.commit()

    # ─── Cached payloads ─────────────────────────────────
    def get_cached_payloads(self, source: str, item_ids: list[str]) -> dict[str, dict]:
        """Bulk-fetch cached payloads for a set of IDs. Missing IDs are simply absent from the result."""
        if not item_ids:
            return {}
        with self._lock:
            placeholders = ",".join("?" * len(item_ids))
            cur = self._conn.execute(
                f"SELECT item_id, payload_json FROM cached_items "
                f"WHERE source = ? AND item_id IN ({placeholders})",
                (source, *item_ids),
            )
            return {row[0]: json.loads(row[1]) for row in cur.fetchall()}

    def put_cached(self, source: str, item_id: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO cached_items (source, item_id, payload_json) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(source, item_id) DO UPDATE SET "
                "  payload_json = excluded.payload_json, fetched_at = datetime('now')",
                (source, item_id, json.dumps(payload, default=str)),
            )
            self._conn.commit()

    def list_cached(self, source: str, limit: int = 200) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT payload_json FROM cached_items WHERE source = ? "
                "ORDER BY fetched_at DESC LIMIT ?",
                (source, limit),
            )
            return [json.loads(row[0]) for row in cur.fetchall()]


cursor_store = CursorStore(config.DATA_DIR / "state" / "cursors.sqlite")
