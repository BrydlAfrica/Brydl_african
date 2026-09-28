"""Blind-consensus evaluation for RLHF tasks.

A consensus group holds N sibling tasks carrying the SAME prompt (see
ConsensusGroup). Each sibling is worked independently — the annotator
never sees the others' work. When every sibling has a submission, the
group is evaluated:

- ``agreed``  — every sibling's latest submission carries the same
  verdict key (preference: chosen a/b; classification: label).
- ``conflict`` — verdicts disagree, or a verdict has no comparable key
  (free-text task types) — a human must arbitrate.
- ``pending`` — not all siblings have answered yet.

A ``resolved`` group is frozen: the auditor's binding decision in the
Conflicts view outlives later revisions. Re-evaluation happens on
annotator submission and on QA verdicts, never raising — evaluation is
bookkeeping and must not break an annotator's successful submit.
"""

import json
import logging

from extensions import db
from models import ConsensusGroup, Submission, Task

logger = logging.getLogger(__name__)

# Statuses a group may hold while still being re-evaluated.
EVALUABLE_STATUSES = ("pending", "agreed", "conflict")


def verdict_key(output_data: str) -> str | None:
    """Extract the comparable verdict from a submission's output.

    Args:
        output_data: The submission's stored JSON document.

    Returns:
        A normalised comparison key (lowercase), or None when the
        answer type carries no deterministic key (free text).
    """
    try:
        doc = json.loads(output_data or "")
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    kind = doc.get("kind")
    if kind == "preference":
        chosen = str(doc.get("chosen", "")).strip().lower()
        return chosen if chosen in ("a", "b") else None
    if kind == "classification":
        label = str(doc.get("label", "")).strip().lower()
        return label or None
    if kind == "image_labeling":
        # Bounding-box / region-labeling: two annotators agree when
        # they pick the same primary LABEL (bbox geometry may vary
        # and is arbitrated — same contract as classification but
        # for the media labeling surface).
        label = str(doc.get("label", "")).strip().lower()
        return label or None
    if kind == "audio_transcription":
        # Free-form transcripts never lock to a deterministic key —
        # identical transcripts are vanishingly rare and would be a
        # false positive if they did. Always arbitrate: return None
        # so identical outputs still drop to the conflict queue.
        return None
    return None


def _latest_submission(task_id: int) -> Submission | None:
    """Return the newest submission for a task (revisions win).

    Args:
        task_id: Primary key of the sibling task.

    Returns:
        The latest Submission by id, or None when none exists.
    """
    return db.session.scalar(
        db.select(Submission)
        .where(Submission.task_id == task_id)
        .order_by(Submission.id.desc())
        .limit(1)
    )


def refresh_group(group_id: int) -> str | None:
    """Re-evaluate a consensus group and persist its status.

    Resolved groups are never touched. If any sibling still lacks a
    submission the group returns to pending (a rejected sibling being
    redone, for example). Evaluation failures are logged, never raised.

    Args:
        group_id: Primary key of the ConsensusGroup.

    Returns:
        The new status, or None when the group could not be evaluated.
    """
    try:
        group = db.session.get(ConsensusGroup, group_id)
        if group is None or group.status == "resolved":
            return None
        tasks = [
            t for t in group.tasks
        ] if group.tasks else db.session.scalars(
            db.select(Task).where(
                Task.consensus_group_id == group_id
            )
        ).all()
        latest_by_task = {
            task.id: _latest_submission(task.id) for task in tasks
        }
        # A sibling whose LATEST submission was rejected is being
        # redone — its verdict is withdrawn and the round waits.
        standing = {
            task_id: (sub if sub is not None
                      and sub.status != "rejected" else None)
            for task_id, sub in latest_by_task.items()
        }
        if any(sub is None for sub in standing.values()):
            new_status = "pending"
        else:
            keys = [verdict_key(sub.output_data)
                    for sub in standing.values()]
            if any(key is None for key in keys):
                new_status = "conflict"
            elif len(set(keys)) == 1:
                new_status = "agreed"
            else:
                new_status = "conflict"
        group.status = new_status
        return new_status
    except Exception:
        logger.exception(
            "Consensus evaluation failed for group %s.", group_id
        )
        return None
