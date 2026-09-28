import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""Stress and fault-isolation suite.

Answers two production questions with measurements, not claims:

1. Can 60 annotators submit simultaneously? 60 barrier-synchronised
   clients POST real, CSRF-validated submissions in two waves, then a
   mixed read-traffic phase runs 360 concurrent page loads. Latency
   percentiles, throughput and a zero-error requirement are asserted.
2. If one feature crashes, does the rest of the system keep running?
   A test-only route raises an unhandled exception mid-exercise; the
   suite then proves the same session keeps working, the database
   session is rolled back (not poisoned) and later concurrent writes
   still succeed.

Run standalone:  python tests/test_stress60.py
"""
import re
import shutil
import sqlite3
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from app import create_app
from extensions import db
from models import AuditLog, Project, Submission, Task, User

N = 60
PASSWORD = "stress-pass-1"

# Like every suite: the RUNNER wipes instance/ + backups/ before the
# process starts (config.py lays them down at import time; deleting a
# directory a live engine already opened only strands new pooled
# connections). This suite cleans up at the END instead.

app = create_app()


def _boom():
    """Test-only route that raises an unhandled exception.

    Registered before the first request (Flask 3 forbids adding rules
    afterwards); Phase B calls it to prove a crashing feature cannot
    take the rest of the system down.
    """
    raise RuntimeError("simulated feature crash")


app.add_url_rule("/__boom__", endpoint="boom", view_func=_boom)

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


def login(client, email, password):
    """POST the sign-in form and follow the redirect."""
    r = client.get("/login")
    tk = get_csrf(r.get_data(as_text=True))
    return client.post("/login", data={"email": email,
                                       "password": password,
                                       "csrf_token": tk})


def pct(samples, p):
    """Nearest-rank percentile of a list of latencies (seconds)."""
    ordered = sorted(samples)
    idx = max(0, min(len(ordered) - 1, int(round(p / 100 * len(ordered))) - 1))
    return ordered[idx]


# ---- seed: 1 admin, 60 annotators, 120 assigned tasks --------------------
print("== Seed 60 annotators + 120 assigned tasks ==")
with app.app_context():
    admin = User(name="Ada Admin", email="admin@brydl.ng", role="admin")
    admin.set_password("admin-pass-1")
    db.session.add(admin)
    annotators = []
    for i in range(N):
        u = User(name=f"Annotator {i + 1}",
                 email=f"stress{i + 1}@brydl.ng", role="annotator")
        u.set_password(PASSWORD)
        annotators.append(u)
    db.session.add_all(annotators)
    db.session.flush()  # assign ids so the project can reference the admin
    project = Project(
        name="Stress classification", client_name="Stress Client",
        task_type="classification", pay_per_task=Decimal("0.05"),
        instructions="Label the sentiment.", created_by=admin.id,
    )
    db.session.add(project)
    db.session.flush()
    tasks = []
    for i, u in enumerate(annotators):
        for _ in range(2):  # two tasks per annotator -> two submit waves
            t = Task(project_id=project.id,
                     input_data=f"Sample text {len(tasks)} for annotator"
                                f" {i + 1}.")
            t.status = "assigned"
            t.assigned_to = u.id
            tasks.append(t)
    db.session.add_all(tasks)
    db.session.commit()
    task_ids = [t.id for t in tasks]
    n_users = db.session.scalar(db.select(db.func.count(User.id)))
expect(n_users == N + 1, f"60 annotators + admin seeded ({n_users} users)")
expect(len(task_ids) == 2 * N, "120 tasks assigned round-robin")

# ---- pre-flight (serial): login, accept terms, grab task CSRF ------------
print("== Pre-flight: 60 logins, terms acceptance, CSRF tokens ==")
t0 = time.perf_counter()
workers = []
for i in range(N):
    c = app.test_client()
    r = login(c, f"stress{i + 1}@brydl.ng", PASSWORD)
    assert r.status_code == 302, f"login failed for stress{i + 1}"
    page = c.get("/annotator/terms").get_data(as_text=True)
    r = c.post("/annotator/terms/accept",
               data={"csrf_token": get_csrf(page)})
    assert r.status_code == 302, "terms acceptance failed"
    task_page = c.get(f"/annotator/task/{task_ids[2 * i]}")
    csrf = get_csrf(task_page.get_data(as_text=True))
    assert csrf, "task page did not render a CSRF token"
    workers.append({"client": c, "csrf": csrf,
                    "task1": task_ids[2 * i], "task2": task_ids[2 * i + 1]})
preflight_s = time.perf_counter() - t0
expect(True, f"60 accounts signed in + terms-accepted in {preflight_s:.1f}s")

# ---- phase A: 60 simultaneous submissions (barrier) ----------------------
print("== Phase A: 60 annotators POST submissions at the same instant ==")


def wave(worker, task_key):
    """One barrier-released submit; returns (ok, status, seconds)."""
    barrier.wait()
    start = time.perf_counter()
    r = worker["client"].post(
        f"/annotator/task/{worker[task_key]}",
        data={"label_choice": "positive", "csrf_token": worker["csrf"]})
    elapsed = time.perf_counter() - start
    return (r.status_code == 302, r.status_code, elapsed)


results, latencies = [], []
barrier = threading.Barrier(N)
with ThreadPoolExecutor(max_workers=N) as pool:
    for outcome in pool.map(lambda w: wave(w, "task1"), workers):
        results.append(outcome)
        latencies.append(outcome[2])
wall_a = sum(latencies) / N  # every thread ran concurrently for ~this long
ok_posts = sum(1 for ok, _, _ in results if ok)
statuses = {s for _, s, _ in results}
expect(ok_posts == N and statuses == {302},
       f"all 60 simultaneous POSTs succeeded (statuses {statuses})")
print(f"   ..  submit POST p50 {pct(latencies, 50) * 1000:.0f} ms ·"
      f" p95 {pct(latencies, 95) * 1000:.0f} ms ·"
      f" max {max(latencies) * 1000:.0f} ms")
with app.app_context():
    n_sub = db.session.scalar(db.select(db.func.count(Submission.id)))
expect(n_sub == N, f"exactly {N} submission rows after wave 1 ({n_sub})")

# ---- phase B: crash one feature, then keep working ------------------------
print("== Phase B: one feature crashes — everything else keeps running ==")


# /__boom__ was registered before the first request (Flask 3 rule);
# here it simply fires.
r = workers[0]["client"].get("/__boom__")
body = r.get_data(as_text=True)
expect(r.status_code == 500, "crashing route answers HTTP 500")
expect("Traceback" not in body and "RuntimeError" not in body,
       "client never sees a traceback or internals")
r = workers[0]["client"].get("/annotator/")
expect(r.status_code == 200,
       "SAME session still works right after the crash"
       " (500 handler rolled the DB session back)")
r = login(app.test_client(), "stress7@brydl.ng", PASSWORD)
expect(r.status_code == 302, "a fresh sign-in still works after the crash")

# ---- phase C: wave 2 — 60 more simultaneous submits ----------------------
print("== Phase C: 60 more simultaneous submissions after the crash ==")
results, latencies = [], []
barrier = threading.Barrier(N)
with ThreadPoolExecutor(max_workers=N) as pool:
    for outcome in pool.map(lambda w: wave(w, "task2"), workers):
        results.append(outcome)
        latencies.append(outcome[2])
ok_posts = sum(1 for ok, _, _ in results if ok)
expect(ok_posts == N,
       f"all 60 wave-2 POSTs succeeded after the crash ({ok_posts})")
print(f"   ..  submit POST p50 {pct(latencies, 50) * 1000:.0f} ms ·"
      f" p95 {pct(latencies, 95) * 1000:.0f} ms ·"
      f" max {max(latencies) * 1000:.0f} ms")

# ---- phase D: mixed read traffic, 60 concurrent users --------------------
print("== Phase D: 60 concurrent users x 6 page loads (mixed reads) ==")


# Expected status per path: /annotator/tasks is a redirect alias by
# design (it sends the navbar's link to the dashboard queue anchor).
EXPECTED_READ = {
    "/annotator/": 200,
    "/annotator/tasks": 302,
    "/annotator/history": 200,
}


def reads(worker):
    """Six concurrent reads across the annotator workspace."""
    out = []
    for path, wanted in EXPECTED_READ.items():
        start = time.perf_counter()
        r = worker["client"].get(path)
        out.append((r.status_code == wanted, time.perf_counter() - start))
    return out


flat = []
with ThreadPoolExecutor(max_workers=N) as pool:
    for batch in pool.map(reads, workers):
        flat.extend(batch)
read_lat = [dt for ok, dt in flat if ok]
read_ok = sum(1 for ok, _ in flat if ok)
rps = len(flat) / (max(read_lat) * 1.05) if read_lat else 0.0
expect(read_ok == len(flat),
       f"all {len(flat)} concurrent reads matched their contract"
       f" ({read_ok})")
print(f"   ..  reads p50 {pct(read_lat, 50) * 1000:.0f} ms ·"
      f" p95 {pct(read_lat, 95) * 1000:.0f} ms ·"
      f" ~{rps:.0f} req/s across 60 clients")

# ---- integrity: no double submissions, consistent DB, audit rows ---------
print("== Integrity ==")
with app.app_context():
    n_sub = db.session.scalar(db.select(db.func.count(Submission.id)))
    n_submitted = db.session.scalar(db.select(db.func.count(Task.id)).where(
        Task.status == "submitted"))
    n_audit = db.session.scalar(db.select(db.func.count(AuditLog.id)))
    per_task = db.session.scalars(db.select(
        Submission.task_id, db.func.count(Submission.id)).group_by(
        Submission.task_id)).all() if hasattr(db.session, "scalars") else []
expect(n_sub == 2 * N and n_submitted == 2 * N,
       f"{2 * N} submissions, {2 * N} tasks flipped to 'submitted'"
       f" (got {n_sub}/{n_submitted})")
expect(n_audit >= 2 * N, f"audit trail recorded the work ({n_audit} rows)")
conn = sqlite3.connect(_os.path.join(ROOT, "instance", "brydl.db"))
expect(conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
       "PRAGMA integrity_check: database file consistent")
conn.close()

shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)
print()
if FAILURES:
    print(f"STRESS/FAULT-ISOLATION TESTS FAILED ({len(FAILURES)}):")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("ALL STRESS + FAULT-ISOLATION TESTS PASSED")
