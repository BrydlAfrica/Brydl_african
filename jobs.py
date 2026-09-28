"""DB-backed background job queue (Phase 10).

Durable, dependency-free deferred work: jobs are rows in
background_jobs, executed by an in-process runner thread inside the
web service (the $0 deployment) — or by a future dedicated worker
service running worker.py, with zero code change, because claiming
is a conditional UPDATE (queued -> running) that only one poller can
win per row.

Design rules:
- Jobs carry identifiers, never secrets (no bank data, no raw
  tokens, no Fernet material).
- Failures retry with exponential backoff up to the row's
  max_attempts, then park as 'failed' with the last error recorded;
  a terminal failure is audited (counts only) and logged.
- Unknown job kinds fail immediately (no retry) — a code rollback
  must not leave a row looping forever.
- Every dispatcher function receives the parsed payload dict and is
  responsible for its own try/except-free contract: it may raise;
  the runner converts exceptions into retries.

Built-in kinds:
- "email.deliver" {to, subject, body} — notification email off the
  request path (enqueued by notifications.notify when jobs run).
- "paystack.reconcile" {} — one sweep over payouts still 'pending'
  with a transfer reference: verified success settles exactly like
  the webhook (same fields, same idempotency), explicit failure
  marks the row failed, anything else is left for the next sweep or
  the webhook. No bank/account data is involved or stored.
"""

import json
import logging
import threading
import time
from datetime import timedelta

from flask import current_app

from audit import log_event
from extensions import db
from models import BackgroundJob, Payment, utc_now

logger = logging.getLogger(__name__)

# The dispatch table; worker.py and tests share it.
RUNNERS = {}

_runner_started = threading.Event()


def enqueue(kind: str, payload: dict | None = None,
            *, max_attempts: int | None = None,
            run_after=None) -> BackgroundJob | None:
    """Queue one job row; best-effort, never raises.

    Args:
        kind: A RUNNERS key.
        payload: Small JSON-safe dict of identifiers.
        max_attempts: Override the configured default (1-20).
        run_after: Optional delay anchor (DateTime, UTC).

    Returns:
        The persisted BackgroundJob, or None when persistence
        failed (the caller's business action is never blocked by
        queueing).
    """
    attempts_cap = int(current_app.config.get("JOB_MAX_ATTEMPTS", 5))
    try:
        job = BackgroundJob(
            kind=kind,
            payload=json.dumps(payload or {}, ensure_ascii=False),
            max_attempts=max_attempts or attempts_cap,
            run_after=run_after or utc_now(),
        )
        db.session.add(job)
        db.session.commit()
        return job
    except Exception:
        db.session.rollback()
        logger.exception("Could not enqueue job %s.", kind)
        return None


def _run_email_deliver(payload: dict) -> None:
    """Send one notification email (the SMTP leg, off the hot path).

    Args:
        payload: {"to": recipient, "subject": ..., "body": ...}.
    """
    from mailer import send_notification_email

    send_notification_email(
        str(payload.get("to") or ""),
        str(payload.get("subject") or ""),
        str(payload.get("body") or payload.get("subject") or ""),
    )


def _run_paystack_reconcile(payload: dict) -> None:
    """Sweep pending Paystack payouts whose webhook has not landed.

    For every payment still 'pending' carrying a transfer reference,
    the gateway's own status decides: success settles exactly like
    the webhook (same fields; the pending-guard keeps it idempotent
    against a late webhook), failure marks the row failed, anything
    else waits. When Paystack is unconfigured or unreachable the
    sweep simply reports and finishes — never raises.
    """
    import paystack as paystack_gateway

    secret = current_app.config.get("PAYSTACK_SECRET_KEY", "")
    base_url = current_app.config.get("PAYSTACK_BASE_URL") \
        or "https://api.paystack.co"
    if not paystack_gateway.is_enabled(secret):
        logger.info("Reconcile skipped: Paystack not configured.")
        return
    cutoff_seconds = int(
        current_app.config.get(
            "RECONCILE_MIN_AGE_SECONDS", 600
        )
    )
    cutoff = utc_now() - timedelta(seconds=cutoff_seconds)
    pending = db.session.scalars(
        db.select(Payment).where(
            Payment.status == "pending",
            Payment.paystack_reference.is_not(None),
            Payment.created_at < cutoff,
        ).order_by(Payment.id.asc()).limit(50)
    ).all()
    settled = failed = skipped = errors = 0
    for payment in pending:
        reference = payment.paystack_reference or ""
        ok, status = paystack_gateway.verify_transfer(
            secret, reference, base_url=base_url
        )
        if not ok:
            errors += 1
            logger.warning(
                "Reconcile could not verify %s: %s", reference, status
            )
            continue
        # Fresh read + pending guard: a webhook that landed while we
        # were in flight wins and this row becomes a no-op.
        db.session.refresh(payment)
        if payment.status != "pending":
            skipped += 1
            continue
        if status == "success":
            payment.status = "paid"
            payment.payment_method = "paystack"
            payment.paid_at = utc_now()
            settled += 1
        elif status in ("failed", "reversed"):
            payment.status = "failed"
            failed += 1
        else:
            skipped += 1
            continue
        try:
            log_event(
                "payout.reconciled",
                actor_name="paystack-reconcile",
                entity_type="payment",
                entity_id=payment.id,
                detail={"status": status, "reference": reference},
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            errors += 1
            logger.exception(
                "Reconcile settlement failed for %s.", reference
            )
    logger.info(
        "Reconcile sweep: %s settled, %s failed, %s waiting,"
        " %s errors.", settled, failed, skipped, errors,
    )


RUNNERS["email.deliver"] = _run_email_deliver
RUNNERS["paystack.reconcile"] = _run_paystack_reconcile


def claim_job(job_id: int) -> BackgroundJob | None:
    """Atomically claim one queued job for this poller.

    Args:
        job_id: The candidate row id.

    Returns:
        The job with status now 'running', or None when another
        poller won the row (or it vanished).
    """
    from sqlalchemy import update

    result = db.session.execute(
        update(BackgroundJob)
        .where(
            BackgroundJob.id == job_id,
            BackgroundJob.status == "queued",
        )
        .values(status="running", attempts=BackgroundJob.attempts + 1,
                updated_at=utc_now())
    )
    if result.rowcount != 1:
        db.session.rollback()
        return None
    db.session.commit()
    return db.session.get(BackgroundJob, job_id)


def run_job(job: BackgroundJob) -> None:
    """Execute one claimed job and record the outcome.

    Success -> done. A raise retries with exponential backoff (30s,
    1m, 2m, ... capped at 1h) until max_attempts, then the row parks
    as 'failed' with the last error and an audit entry. Unknown kinds
    park immediately.

    Args:
        job: The claimed (running) job row.
    """
    runner = RUNNERS.get(job.kind)
    if runner is None:
        # Unknown kinds park immediately: a code rollback must never
        # leave a row looping until its attempt cap.
        try:
            job = db.session.get(BackgroundJob, job.id)
            job.status = "failed"
            job.last_error = f"Unknown job kind {job.kind!r}."
            log_event(
                "job.failed",
                actor_name="jobs",
                entity_type="background_job",
                entity_id=job.id,
                detail={"kind": job.kind,
                        "attempts": job.attempts},
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception(
                "Could not park unknown job %s.", job.id
            )
        return
    try:
        payload = json.loads(job.payload or "{}")
        if not isinstance(payload, dict):
            raise ValueError("Job payload must be a JSON object.")
        runner(payload)
    except Exception as exc:
        db.session.rollback()
        try:
            job = db.session.get(BackgroundJob, job.id)
            job.last_error = str(exc)[:500]
            if job.attempts >= job.max_attempts:
                job.status = "failed"
                log_event(
                    "job.failed",
                    actor_name="jobs",
                    entity_type="background_job",
                    entity_id=job.id,
                    detail={"kind": job.kind,
                            "attempts": job.attempts},
                )
            else:
                backoff = min(30 * (2 ** (job.attempts - 1)), 3600)
                job.status = "queued"
                job.run_after = utc_now() + timedelta(seconds=backoff)
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception(
                "Could not record failure for job %s.", job.id
            )
        return
    try:
        job = db.session.get(BackgroundJob, job.id)
        job.status = "done"
        job.last_error = None
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Could not mark job %s done.", job.id)


def run_due_once(app, limit: int = 10) -> int:
    """Run every currently-due job once, synchronously.

    The deterministic entrypoint for tests and the CLI; the live
    runner thread calls it on its poll loop.

    Args:
        app: The Flask application (provides the context).
        limit: Maximum jobs to process in this pass.

    Returns:
        The number of jobs processed.
    """
    from sqlalchemy import select

    with app.app_context():
        try:
            candidates = db.session.scalars(
                select(BackgroundJob.id)
                .where(
                    BackgroundJob.status == "queued",
                    BackgroundJob.run_after <= utc_now(),
                )
                .order_by(BackgroundJob.run_after.asc(),
                          BackgroundJob.id.asc())
                .limit(limit)
            ).all()
        except Exception:
            db.session.rollback()
            logger.exception("Job poll query failed.")
            return 0
        processed = 0
        for job_id in candidates:
            if processed >= limit:
                break
            job = claim_job(job_id)
            if job is None:
                continue  # another poller won it
            processed += 1
            run_job(job)
        return processed


def _runner_loop(app, poll_seconds: int) -> None:
    """Background thread body: poll, run due jobs, sleep, repeat.

    Args:
        app: The Flask application.
        poll_seconds: Seconds between passes.
    """
    while True:
        try:
            run_due_once(app, limit=10)
        except Exception:
            logger.exception("Runner pass failed; continuing.")
        time.sleep(poll_seconds)


def start_runner(app) -> bool:
    """Start the in-process runner thread (once per process).

    Controlled by JOBS_ENABLED and the battery's BRYDL_DISABLE_JOBS
    kill switch. Daemon-threaded: it never blocks shutdown, and rows
    left 'running' by a killed process are requeued below.

    Args:
        app: The Flask application.

    Returns:
        True when a runner was started.
    """
    if not app.config.get("JOBS_ENABLED", True):
        return False
    if not _requeue_stuck(app):
        logger.warning("Runner skipped: could not heal stale jobs.")
        return False
    if _runner_started.is_set():
        return False
    _runner_started.set()
    poll = int(app.config.get("JOBS_POLL_SECONDS", 30))
    thread = threading.Thread(
        target=_runner_loop,
        args=(app, poll),
        name="brydl-jobs",
        daemon=True,
    )
    thread.start()
    app.logger.info(
        "Background job runner started (poll %ss).", poll
    )
    return True


def _requeue_stuck(app) -> bool:
    """Requeue jobs left 'running' by a dead process.

    A redeploy or crash between claim and completion leaves rows in
    'running'; on boot they go back to 'queued' (jobs are written to
    be safe to re-run: emails may double-send in that rare window,
    settlement is pending-guarded and idempotent).

    Args:
        app: The Flask application.

    Returns:
        True when the healing pass succeeded.
    """
    from sqlalchemy import update

    with app.app_context():
        try:
            db.session.execute(
                update(BackgroundJob)
                .where(BackgroundJob.status == "running")
                .values(status="queued", updated_at=utc_now())
            )
            db.session.commit()
            return True
        except Exception:
            db.session.rollback()
            logger.exception("Stale job healing failed.")
            return False
