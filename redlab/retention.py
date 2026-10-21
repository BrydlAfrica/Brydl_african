"""RedLab transcript retention and automated deletion policy (DPA compliance).

Implements automated pruning of concluded and archived RedLab sessions
older than a configurable retention window (e.g. 90 days), triggering DB foreign
key ON DELETE CASCADE deletion of all associated Message transcripts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from extensions import db
from redlab.models import Session

logger = logging.getLogger(__name__)


def prune_expired_transcripts(days: int = 90, project_id: Optional[str] = None) -> int:
    """Delete concluded sessions older than `days` days.

    Returns the count of pruned sessions.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    query = db.session.query(Session).filter(
        Session.status == "concluded",
        Session.created_at < cutoff,
    )
    if project_id:
        query = query.filter(Session.project_ref == str(project_id))

    sessions_to_prune = query.all()
    count = len(sessions_to_prune)
    if count == 0:
        return 0

    for s in sessions_to_prune:
        db.session.delete(s)
    db.session.commit()
    logger.info("RedLab retention: Pruned %d expired sessions older than %d days.", count, days)
    return count
