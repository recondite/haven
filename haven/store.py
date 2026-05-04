"""Filesystem layout helpers for the Markdown store."""
from haven import config


def ensure_dirs() -> None:
    """Create the data/ tree on startup. Idempotent."""
    (config.DATA_DIR / "secrets").mkdir(parents=True, exist_ok=True)
    (config.DATA_DIR / "agents" / "gmail" / "items").mkdir(parents=True, exist_ok=True)
    (config.DATA_DIR / "contacts" / "people").mkdir(parents=True, exist_ok=True)
    (config.DATA_DIR / "companies" / "orgs").mkdir(parents=True, exist_ok=True)
    (config.DATA_DIR / "ars" / "open").mkdir(parents=True, exist_ok=True)
    (config.DATA_DIR / "ars" / "done").mkdir(parents=True, exist_ok=True)
    (config.DATA_DIR / "state" / "queues").mkdir(parents=True, exist_ok=True)
