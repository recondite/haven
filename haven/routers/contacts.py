"""Cross-source contact list route."""
from __future__ import annotations

from fastapi import APIRouter

from haven import config
from haven import contacts as contacts_mod
from haven.db import cursor_store

router = APIRouter(tags=["contacts"])


@router.get("/api/contacts")
async def list_contacts() -> dict:
    """Cross-source contact list, derived live from every cached item.

    No DB writes, no LLM. Cheap because it's a single in-memory pass over the
    same data the items endpoints serve.
    """
    self_email = "garth@ayarlabs.com"
    items: list[dict] = []
    for src in config.KNOWN_SOURCES:
        for it in cursor_store.list_cached(src):
            if it.get("filter_status") == "reject":
                continue
            items.append(it)
    derived = contacts_mod.derive_contacts(items, self_email=self_email)
    return {"contacts": [c.to_dict() for c in derived], "total": len(derived)}
