"""The decisions you implement.

The graph is already wired. Finish these functions so each check stays in
application code, not in the model. Keep the function names and the failure
codes exactly as written. Use contracts.failed(...) for every failure.
"""
from .contracts import Settings, failed


def load_settings(values: dict | None = None) -> Settings:
    """Build Settings from a dictionary.

    Reject max_model_calls below 1 with ValueError whose message contains
    "at least 1". A zero-call setting must fail before any workflow starts.
    Missing keys keep the Settings defaults: 3 calls, 6000 token budget,
    2000 tokens reserved per call, and a 900 second approval window.
    """
    settings = Settings(**(values or {}))
    if settings.max_model_calls < 1:
        raise ValueError("max_model_calls must be at least 1")
    return settings


def check_access(principal, tenant_id: str):
    """Return None when this identity may read the case.

    Refuse with failed("ACCESS_DENIED", ...) when the principal's tenant_id
    differs from tenant_id, or when "case:read" is missing from permissions.
    A viewer and another company's employee both stop here, before any model call.
    """
    if principal.tenant_id != tenant_id or "case:read" not in principal.permissions:
        return failed("ACCESS_DENIED", "You may not read this case.")
    return None


def reserve_call(state: dict, settings: Settings) -> dict:
    """Admit one model call, or stop with a typed failure.

    Read model_calls and reserved_tokens from state. Missing values mean 0.

    If model_calls is already at settings.max_model_calls, return
    failed("ITERATION_LIMIT", ...). Do not increment the counters.

    If reserved_tokens + settings.reserve_tokens would pass
    settings.total_token_budget, return failed("TOKEN_BUDGET", ...).
    Do not increment the counters.

    Otherwise return a partial update:
    model_calls increased by 1, reserved_tokens increased by
    settings.reserve_tokens, and status "planning".
    """
    calls = state.get("model_calls") or 0
    reserved = state.get("reserved_tokens") or 0
    if calls >= settings.max_model_calls:
        return failed("ITERATION_LIMIT", "The model call limit has been reached.")
    if reserved + settings.reserve_tokens > settings.total_token_budget:
        return failed("TOKEN_BUDGET", "The token budget would be exceeded.")
    return {
        "model_calls": calls + 1,
        "reserved_tokens": reserved + settings.reserve_tokens,
        "status": "planning",
    }


def validate_proposal(evidence: dict, proposal: dict):
    """Return None when the proposal may be shown to a manager.

    The model drafts the summary. Your code checks the facts.

    Return failed("EVIDENCE_MISMATCH", ...) unless all of these are true:
    - proposal["action"] is exactly "create_review_task"
    - proposal["case_id"] equals evidence["case_id"]
    - proposal["invoice_id"] equals evidence["invoice_id"]
    - both of those identifiers appear in proposal["evidence_refs"]

    A convincing summary cannot replace a matching invoice id.
    """
    case_id = evidence["case_id"]
    invoice_id = evidence["invoice_id"]
    refs = proposal.get("evidence_refs") or []
    if (
        proposal.get("action") != "create_review_task"
        or proposal.get("case_id") != case_id
        or proposal.get("invoice_id") != invoice_id
        or case_id not in refs
        or invoice_id not in refs
    ):
        return failed("EVIDENCE_MISMATCH", "The proposal does not match the case evidence.")
    return None


def approval_decision(principal, reply: dict, expected_hash: str, now: int, expires_at: int, tenant_id: str) -> dict:
    """Decide what the current person is allowed to do with this exact proposal.

    Check in this order:

    1. reply["approved"] must be a real boolean. The string "yes" is not enough.
       Return {"ok": False, "failure": failed("INVALID_APPROVAL", ...)}.
    2. reply["proposal_hash"] must equal expected_hash.
       Return {"ok": False, "failure": failed("STALE_APPROVAL", ...)}.
    3. principal.permissions must include "task:approve", and
       principal.tenant_id must equal tenant_id.
       Return {"ok": False, "failure": failed("APPROVAL_FORBIDDEN", ...)}.
       The approver comes from the current principal, never from model text.
    4. If now >= expires_at, return
       {"ok": False, "failure": failed("APPROVAL_EXPIRED", ...)}.
    5. If approved is True, return
       {"ok": True, "decision": "approved", "approved_by": principal.subject}.
    6. If approved is False, return {"ok": True, "decision": "rejected"}.
    """
    approved = reply.get("approved")
    if not isinstance(approved, bool):
        return {"ok": False, "failure": failed("INVALID_APPROVAL", "Approval must be true or false.")}
    if reply.get("proposal_hash") != expected_hash:
        return {"ok": False, "failure": failed("STALE_APPROVAL", "The approval does not match this proposal.")}
    if "task:approve" not in principal.permissions or principal.tenant_id != tenant_id:
        return {"ok": False, "failure": failed("APPROVAL_FORBIDDEN", "You may not approve this task.")}
    if now >= expires_at:
        return {"ok": False, "failure": failed("APPROVAL_EXPIRED", "The approval window has closed.")}
    if approved:
        return {"ok": True, "decision": "approved", "approved_by": principal.subject}
    return {"ok": True, "decision": "rejected"}


def action_key(tenant_id: str, request_id: str, proposal_hash: str) -> str:
    """Stable identity for one approved action.

    Return tenant_id, request_id, and proposal_hash joined by colons:
    "acme-in:request-1:<64-character hash>".
    The same proposal must keep the same key after a restart.
    """
    return f"{tenant_id}:{request_id}:{proposal_hash}"


class TaskLedger:
    """In-memory stand-in for the external task system."""

    def __init__(self):
        self._rows: dict[str, dict] = {}

    def create(self, key: str, payload: dict) -> dict:
        """Create one task, or return the receipt that already exists for key.

        Same key and same payload: return the original receipt. Do not add a row.
        Same key and a different payload: raise ValueError. The message must
        contain "payload".
        New key: store a copy of payload and return
        {"action_key": key, "task_id": "TASK-1"} for the first task,
        "TASK-2" for the second, and so on.
        """
        row = self._rows.get(key)
        if row is not None:
            if row["payload"] != payload:
                raise ValueError(f"Action key {key} was already used with a different payload.")
            return dict(row["receipt"])
        receipt = {"action_key": key, "task_id": f"TASK-{len(self._rows) + 1}"}
        self._rows[key] = {"payload": dict(payload), "receipt": receipt}
        return dict(receipt)

    def all(self) -> list[dict]:
        """Return the receipts in insertion order."""
        return [dict(row["receipt"]) for row in self._rows.values()]
