"""Nightly local snapshots of Haven's SQLite stores (M0.2, build plan v2).

The spine is the accountability record for real outbound sends; before this
module existed one disk fault would have erased it. `VACUUM INTO` produces a
consistent snapshot even with live WAL connections. Backups are append-only
per ground rules — never pruned by Haven; total size is surfaced on /system
so GT decides when to archive.

SecondBrain backup is deliberately out of scope: Haven must not take write-
ownership of it (recommend git/backup coverage separately).
"""
from __future__ import annotations

import datetime
import logging
import sqlite3

from haven import config

log = logging.getLogger("haven")

BACKUP_DIR = config.DATA_DIR / "backups"
_SOURCES = (
    config.DATA_DIR / "state" / "spine.sqlite",
    config.DATA_DIR / "state" / "cursors.sqlite",
)


def backup_now() -> list[dict]:
    """Snapshot each store to backups/<stem>-YYYY-MM-DD.sqlite. Idempotent per
    day — an existing snapshot for today is left untouched."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.date.today().isoformat()
    results: list[dict] = []
    for src in _SOURCES:
        if not src.exists():
            results.append({"source": src.name, "status": "missing"})
            continue
        dest = BACKUP_DIR / f"{src.stem}-{stamp}.sqlite"
        if dest.exists():
            results.append({"source": src.name, "status": "exists", "file": dest.name})
            continue
        conn = sqlite3.connect(str(src))
        try:
            conn.execute(f"VACUUM INTO '{dest.as_posix()}'")
            results.append({"source": src.name, "status": "created", "file": dest.name,
                            "bytes": dest.stat().st_size})
        except Exception as e:  # noqa: BLE001 — a failed backup must be loud, not fatal
            log.error("backup failed for %s: %s", src.name, e)
            results.append({"source": src.name, "status": "error", "error": str(e)[:200]})
        finally:
            conn.close()
    return results


def backup_status() -> dict:
    """Age + size summary for the /system panel."""
    if not BACKUP_DIR.is_dir():
        return {"count": 0, "latest": None, "total_bytes": 0}
    files = sorted(BACKUP_DIR.glob("*.sqlite"))
    return {
        "count": len(files),
        "latest": max((f.name for f in files), default=None),
        "total_bytes": sum(f.stat().st_size for f in files),
    }
