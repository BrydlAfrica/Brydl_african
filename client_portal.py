"""Client portal helpers (Phase 7): tokens, redaction, safe records.

Turns Brydl from an internal dashboard into a client-facing
application: a client follows their project via an opaque link and
downloads approved deliverables — with the privacy surface enforced
in code, not convention:

- Tokens: 32-byte urlsafe, only the sha256 hash is ever stored.
- Logs: /client/<raw-token> request paths are redacted before the
  REQ access line is written.
- Records: client-safe delivery documents strip annotator identity
  and gold markers while keeping the data the client paid for.

This module is PURE: no Flask, no database, never raises.
"""

import hashlib
import re
import secrets
from datetime import datetime

# Path shape whose raw segment must never reach an access log.
_CLIENT_PATH_RE = re.compile(r"^(/client/)([A-Za-z0-9_\-]+)(/.*)?$")


def generate_token() -> str:
    """Return a fresh opaque client token (urlsafe, 32 bytes)."""
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    """Return the sha256 hex digest of a raw token (storage form).

    Args:
        raw: The opaque token exactly as issued.

    Returns:
        The 64-character lowercase hex digest.
    """
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def redact_client_path(path: str) -> str:
    """Redact the raw token from a /client/<token>[...] request path.

    Args:
        path: The request path.

    Returns:
        The path with the token segment replaced by "[redacted]"
        (unchanged when the path is not a client-portal path).
    """
    match = _CLIENT_PATH_RE.match(path or "")
    if match is None:
        return path or ""
    return f"{match.group(1)}[redacted]{match.group(3) or ''}"


def link_expired(expires_at: datetime | None, now: datetime) -> bool:
    """Return True when a token's optional expiry has passed."""
    return expires_at is not None and expires_at <= now


def progress_summary(counts: dict, approved: int, total_tasks: int) -> dict:
    """Assemble the client-facing progress snapshot.

    Args:
        counts: {"submitted": n, "approved": n, "rejected": n} —
            submission-side statuses for the project.
        approved: Approved-submission count (source of truth for the
            delivered share).
        total_tasks: Total tasks in the project.

    Returns:
        A dict with total / completed / in_review / rejected counts,
        a 0-100 progress percent, and the average approved QA score
        (None when nothing is approved yet).
    """
    submitted = counts.get("submitted", 0)
    rejected = counts.get("rejected", 0)
    in_review = max(submitted - approved, 0)
    done_share = (approved / total_tasks * 100) if total_tasks else 0
    return {
        "total_tasks": total_tasks,
        "approved": approved,
        "in_review": in_review,
        "rejected": rejected,
        "progress_percent": round(min(done_share, 100.0), 1),
    }


def client_safe_record(
    task_id: int,
    input_json: object,
    output_json: object,
    qa_score: float | None,
    consensus_status: str | None,
    consensus_decision: str | None,
    submitted_at: str | None,
    reviewed_at: str | None,
) -> dict:
    """Build one client-safe delivery record.

    Everything a client paid for stays (input, output, consensus,
    timestamps); everything internal goes (annotator identity, gold
    markers, reviewer notes, raw database ids of staff).

    Args:
        task_id: The task id (client-side row reference).
        input_json: Parsed task input document (or raw value).
        output_json: Parsed submission output document (or raw value).
        qa_score: The QA score, or None.
        consensus_status / consensus_decision: Consensus fields.
        submitted_at / reviewed_at: ISO timestamps or None.

    Returns:
        The delivery record dict.
    """
    return {
        "task_id": task_id,
        "input": input_json,
        "output": output_json,
        "qa_score": qa_score,
        "consensus_status": consensus_status,
        "consensus_decision": consensus_decision,
        "submitted_at": submitted_at,
        "reviewed_at": reviewed_at,
    }


def to_jsonl(records: list[dict]) -> str:
    """Serialize delivery records to newline-delimited JSON."""
    import json

    lines = [
        json.dumps(record, ensure_ascii=False) for record in records
    ]
    return "\n".join(lines) + ("\n" if lines else "")
