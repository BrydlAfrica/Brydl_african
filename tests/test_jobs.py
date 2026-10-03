"""Phase 10 suite: the background job queue.

Covers: enqueue + payload persistence, synchronous execution
(run_due_once), claim race-safety (one poller wins), retry with
exponential backoff, terminal failure with audit, unknown kinds
failing fast, the Paystack reconciliation sweep (settled / failed /
waiting / unconfigured) against a stubbed gateway, stale-runner
healing, the notification email leg moving off the request path,
and the runner kill switches (JOBS_ENABLED / BRYDL_DISABLE_JOBS).
"""
from app import create_app as _create
from models import (
    AuditLog,
    BackgroundJob,
    Notification,
    Payment,
    Project,
    Task,
    User,
    utc_now,
)
from extensions import db
from app import create_app
import paystack as paystack_gateway
import jobs
from datetime import timedelta
import sys
import shutil
import re
import json
import os
import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))


app = create_app()

FAILURES = []


def expect(condition, label):
    """Record one granular pass/fail check."""
    if condition:
        print(f"   ok: {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL: {label}")


def get_csrf(html):
    """Pull the CSRF token out of a rendered form page."""
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None


shutil.rmtree("instance", ignore_errors=True)
_sent_inline = []


def _fake_send(recipient, subject, body):
    """Stub for the inline SMTP leg: record, never raise."""
    _sent_inline.append((recipient, subject))


# ---- Seed admin + annotator + a project for payout rows ----
with app.app_context():
    admin = User(name="Ada Admin", email="jobs-admin@brydl.ng",
                 role="admin")
    admin.set_password("admin-pass-1")
    ann = User(name="Ann Work", email="jobs-ann@brydl.ng",
               role="annotator", email_notifications_opt_in=True)
    ann.set_password("ann-pass-1")
    db.session.add_all([admin, ann])
    db.session.commit()


# ---- Section 1: enqueue + execute ----
print("== Section 1: enqueue + execute ==")
with app.app_context():
    job = jobs.enqueue("email.deliver", {
        "to": "ann@brydl.ng", "subject": "Hello",
        "body": "World",
    })
    expect(job is not None and job.status == "queued"
           and json.loads(job.payload)["to"] == "ann@brydl.ng",
           "enqueue persists a queued row with its payload")
    job_id = job.id
    job = None
processed = jobs.run_due_once(app, limit=5)
with app.app_context():
    done = db.session.get(BackgroundJob, job_id)
expect(processed >= 1 and done.status == "done",
       "run_due_once executes and completes the job")
with app.app_context():
    ghost = jobs.enqueue("not.a.kind", {})
    ghost_id = ghost.id
jobs.run_due_once(app, limit=5)
with app.app_context():
    failed = db.session.get(BackgroundJob, ghost_id)
    expect(failed.status == "failed"
           and "Unknown job kind" in (failed.last_error or ""),
           "unknown kind fails immediately (no retry loop)")
    expect(db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "job.failed")) is not None,
        "terminal failure audited")

# ---- Section 2: claiming + retries ----
print("== Section 2: claiming + retries ==")
with app.app_context():
    race = jobs.enqueue("email.deliver", {"to": "x@b.ng",
                                          "subject": "s"})
    race_id = race.id
    claimed = jobs.claim_job(race_id)
    claimed_attempts = claimed.attempts if claimed else None
    lost = jobs.claim_job(race_id)
expect(claimed is not None and lost is None
       and claimed_attempts == 1,
       "conditional claim: exactly one poller wins")
with app.app_context():
    race2 = jobs.enqueue("email.deliver", {"to": "y@b.ng",
                                           "subject": "s"})
    race2_id = race2.id
original_runners = dict(jobs.RUNNERS)
calls = {"n": 0}


def _flaky(payload):
    calls["n"] += 1
    if calls["n"] < 3:
        raise RuntimeError("transient")


jobs.RUNNERS["email.deliver"] = _flaky
with app.app_context():
    row = db.session.get(BackgroundJob, race2_id)
    row.created_at = utc_now() - timedelta(hours=1)
    row.run_after = utc_now() - timedelta(hours=1)
    db.session.commit()
jobs.run_due_once(app, limit=5)
with app.app_context():
    row = db.session.get(BackgroundJob, race2_id)
    expect(row.status == "queued" and row.attempts == 1
           and "transient" in (row.last_error or "")
           and row.run_after > utc_now(),
           "failure requeues with backoff (run_after pushed)")
    row.run_after = utc_now() - timedelta(seconds=1)
    db.session.commit()
jobs.run_due_once(app, limit=5)
with app.app_context():
    row = db.session.get(BackgroundJob, race2_id)
    expect(row.status == "queued" and row.attempts == 2,
           "second failure still retries")
    row.run_after = utc_now() - timedelta(seconds=1)
    db.session.commit()
jobs.run_due_once(app, limit=5)
with app.app_context():
    row = db.session.get(BackgroundJob, race2_id)
    expect(row.status == "done" and calls["n"] == 3,
           "third attempt succeeds and completes")
jobs.RUNNERS.clear()
jobs.RUNNERS.update(original_runners)

# ---- Section 3: Paystack reconcile sweep ----
print("== Section 3: paystack.reconcile ==")
_original_verify = paystack_gateway.verify_transfer
_verify_results = {}


def _stub_verify(secret, reference, base_url="https://api.paystack.co"):
    if reference in _verify_results:
        return True, _verify_results[reference]
    return False, "network down"


paystack_gateway.verify_transfer = _stub_verify
try:
    app.config["PAYSTACK_SECRET_KEY"] = "sk_test_reconcile"
    app.config["RECONCILE_MIN_AGE_SECONDS"] = 2
    with app.app_context():
        admin = db.session.scalar(db.select(User).where(
            User.role == "admin"))
        a_id = admin.id
        # Payout rows reference an annotator (required FK).
        ann = db.session.scalar(db.select(User).where(
            User.email == "jobs-ann@brydl.ng"))

        def _pending_payment(reference, age_minutes):
            p = Payment(
                annotator_id=ann.id,
                amount=12.34, currency="USD",
                period_start=utc_now() - timedelta(days=7),
                period_end=utc_now(),
                tasks_count=3, status="pending",
                payment_method="paystack",
                paystack_reference=reference,
                created_at=utc_now() - timedelta(
                    minutes=age_minutes),
            )
            db.session.add(p)
            db.session.commit()
            return p.id

        settled_id = _pending_payment("brydl-settle-1", 0.2)
        failed_id = _pending_payment("brydl-fail-1", 0.2)
        fresh_id = _pending_payment("brydl-fresh-1", 0)
        db.session.commit()
    _verify_results["brydl-settle-1"] = "success"
    _verify_results["brydl-fail-1"] = "failed"
    with app.app_context():
        job = jobs.enqueue("paystack.reconcile", {})
        job_id = job.id
    import time as _t
    _t.sleep(0.3)
    jobs.run_due_once(app, limit=5)
    with app.app_context():
        paid = db.session.get(Payment, settled_id)
        dead = db.session.get(Payment, failed_id)
        fresh = db.session.get(Payment, fresh_id)
        expect(paid.status == "paid"
               and paid.payment_method == "paystack"
               and paid.paid_at is not None,
               "verified success settles exactly like the webhook")
        expect(dead.status == "failed",
               "gateway failure marks the payout failed")
        expect(fresh.status == "pending",
               "recent payouts are below the sweep cutoff")
        expect(db.session.scalar(db.select(AuditLog).where(
            AuditLog.action == "payout.reconciled")) is not None,
            "reconciliation audited")
    # Idempotency: a re-sweep must not resurrect or double-settle.
    _verify_results["brydl-settle-1"] = "processing"
    jobs.run_due_once(app, limit=5)
    with app.app_context():
        paid = db.session.get(Payment, settled_id)
        expect(paid.status == "paid",
               "re-sweep never touches a settled payout")
    # Unconfigured Paystack: sweep is a safe no-op.
    app.config["PAYSTACK_SECRET_KEY"] = ""
    with app.app_context():
        job = jobs.enqueue("paystack.reconcile", {})
        job_id = job.id
    jobs.run_due_once(app, limit=5)
    with app.app_context():
        done = db.session.get(BackgroundJob, job_id)
        expect(done.status == "done",
               "unconfigured gateway sweeps to a clean no-op")
finally:
    paystack_gateway.verify_transfer = _original_verify
    app.config["PAYSTACK_SECRET_KEY"] = ""

# ---- Section 4: emails off the request path ----
print("== Section 4: queued notification emails ==")
with app.app_context():
    import mailer

    original_send = mailer.send_notification_email
    mailer.send_notification_email = _fake_send
    try:
        app.config["SMTP_HOST"] = "smtp.test"
        os.environ["BRYDL_DISABLE_JOBS"] = ""
        app.config["JOBS_ENABLED"] = True
        ann = db.session.scalar(db.select(User).where(
            User.email == "jobs-ann@brydl.ng"))
        from notifications import notify

        ann_email = ann.email
        notify(ann, "task.assigned", subject="Work arrived",
               body="2 tasks")
        db.session.commit()
        # Capture the just-enqueued row id before the ctx closes.
        q_row = db.session.scalars(
            db.select(BackgroundJob)
            .where(
                BackgroundJob.kind == "email.deliver",
                BackgroundJob.payload.contains(ann_email),
            )
            .order_by(BackgroundJob.id.desc())
        ).first()
        expect(q_row is not None and q_row.status == "queued"
               and json.loads(q_row.payload)["to"] == ann_email,
               "notify enqueues the SMTP leg when jobs run")
        queued_id = q_row.id if q_row is not None else None
    finally:
        os.environ["BRYDL_DISABLE_JOBS"] = "1"
        app.config["SMTP_HOST"] = ""
        app.config["JOBS_ENABLED"] = False
        mailer.send_notification_email = original_send
expect(_sent_inline == [],
       "inline send skipped when the leg is queued")
jobs.run_due_once(app, limit=10)
with app.app_context():
    row = db.session.get(BackgroundJob, queued_id) if queued_id else None
    expect(row is not None and row.status == "done",
           "queued email job executes to done")

# ---- Section 5: kill switches + healing ----
print("== Section 5: switches + healing ==")
app.config["JOBS_ENABLED"] = False
expect(jobs.start_runner(app) is False,
       "JOBS_ENABLED=0 keeps the runner off")
app.config["JOBS_ENABLED"] = True
with app.app_context():
    stuck = jobs.enqueue("email.deliver", {"to": "z@b.ng",
                                           "subject": "s"})
    stuck_id = stuck.id
    from sqlalchemy import update

    db.session.execute(
        update(BackgroundJob)
        .where(BackgroundJob.id == stuck_id)
        .values(status="running")
    )
    db.session.commit()
jobs._requeue_stuck(app)
with app.app_context():
    healed = db.session.get(BackgroundJob, stuck_id)
    expect(healed is not None and healed.status == "queued",
           "boot healing requeues rows stuck in running")
os.environ["BRYDL_DISABLE_JOBS"] = "1"
_os.makedirs(ROOT + "/instance", exist_ok=True)
_os.chdir(ROOT)

fresh = _create()
os.environ["BRYDL_DISABLE_JOBS"] = ""
expect(fresh.test_client().get("/api/v1/ping").status_code == 200,
       "apps boot cleanly with the runner disabled")

print()
if FAILURES:
    print(f"JOB TESTS FAILED ({len(FAILURES)}):")
    for label in FAILURES:
        print(f"  - {label}")
    sys.exit(1)
print("ALL JOB TESTS PASSED")
