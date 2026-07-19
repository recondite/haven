"""Executor — the ONLY module that turns an approved draft into an action.

Phase 1 safe-foundation state: **dry-run only**. `approve()` records the action
as `dry_run` and sends nothing external. The real outbound send (Slack post,
Gmail reply) is deliberately unbuilt (`_send_live` raises) and gated behind
GT sign-off per ground rules #1/#4 — no auto-send code path exists yet.

Guarantees already in force:
- One approval = exactly one action (spine.record_action UNIQUE(draft_id)).
- No action without a real, non-rejected draft (checked here + FK in schema).
- No deletes, ever, on any external service (NO_DELETE denylist, enforced at
  the real-send boundary once it exists).
"""
from __future__ import annotations

import logging

from haven.spine import spine

log = logging.getLogger("haven")

DRY_RUN = True  # flip only with GT sign-off + a live executor implementation

# Outbound verbs the executor may ever perform. Anything destructive is absent
# by construction — there is no delete path to disable.
ALLOWED_KINDS = {"slack", "email", "task", "wiki"}


class ExecutorError(Exception):
    pass


def _send_live(kind: str, target: str, payload: str) -> dict:
    """Real outbound send. Intentionally not implemented — building this is the
    ground-rule-gated step that requires explicit GT approval first."""
    raise NotImplementedError(
        "Live send is not built. Haven can draft and approve (dry-run) only until "
        "the executor's send path is reviewed and signed off."
    )


def approve(draft_id: int, actor: str = "gt") -> dict:
    """Approve a pending draft -> record its single action. Idempotent."""
    draft = spine.get_draft(draft_id)
    if draft is None:
        raise ExecutorError(f"draft {draft_id} not found")
    if draft["status"] == "rejected":
        raise ExecutorError(f"draft {draft_id} was rejected; cannot approve")
    if draft["kind"] not in ALLOWED_KINDS:
        raise ExecutorError(f"draft {draft_id} has disallowed kind {draft['kind']!r}")

    if DRY_RUN:
        result = {"dry_run": True, "note": "no external send performed"}
        status = "dry_run"
    else:  # pragma: no cover - not reachable until sign-off
        result = _send_live(draft["kind"], draft["target"], draft["payload"])
        status = "sent"

    action_id, created = spine.record_action(
        draft_id, draft["kind"], draft["target"], status, result
    )
    if created:
        spine.set_draft_status(draft_id, "approved")
        spine.record_feedback(draft_id, "approved_clean")
        spine.audit(actor, "draft_approved", "draft", draft_id, {"action_id": action_id})
        spine.audit("system", "action_executed", "action", action_id,
                    {"status": status, "dry_run": DRY_RUN})
    return {"draft_id": draft_id, "action_id": action_id, "created": created,
            "status": status, "dry_run": DRY_RUN}


def reject(draft_id: int, reason: str = "", actor: str = "gt") -> dict:
    draft = spine.get_draft(draft_id)
    if draft is None:
        raise ExecutorError(f"draft {draft_id} not found")
    spine.set_draft_status(draft_id, "rejected")
    spine.record_feedback(draft_id, "rejected")
    spine.audit(actor, "draft_rejected", "draft", draft_id, {"reason": reason[:300]})
    return {"draft_id": draft_id, "status": "rejected"}
