"""Export kit tests: dataset export (JSONL/CSV) + bank-ready payout CSV."""
import csv
import io
import json
import os
import re
import shutil
import sys
from decimal import Decimal

ROOT = os.environ.get("BRYDL_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
os.chdir(ROOT)
shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models import (  # noqa: E402
    AuditLog,
    Payment,
    Project,
    Submission,
    Task,
    User,
    money,
    utc_now,
)

FAILURES = []


def expect(condition, message):
    if condition:
        print(f"   ok: {message}")
    else:
        FAILURES.append(message)
        print(f"   FAIL: {message}")


def get_csrf(html):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None


def login(client, email, password):
    page = client.get("/login").get_data(as_text=True)
    return client.post("/login", data={
        "email": email, "password": password,
        "csrf_token": get_csrf(page),
    })


# ---- seed --------------------------------------------------------------
app = create_app()
with app.app_context():
    admin = User(name="Ada Admin", email="admin@brydl.ng", role="admin")
    admin.set_password("admin-pass-1")
    ann_a = User(name="Tunde A", email="a@brydl.ng", role="annotator")
    ann_a.set_password("annotator-pass-1")
    ann_b = User(name="Bisi B", email="b@brydl.ng", role="annotator")
    ann_b.set_password("annotator-pass-2")
    db.session.add_all([admin, ann_a, ann_b])
    db.session.flush()

    proj = Project(name="Export Proj", client_name="Client X",
                   task_type="translation", created_by=admin.id,
                   pay_per_task=money("0.10"),
                   instructions="Translate the text.")
    db.session.add(proj)
    db.session.flush()

    tasks = []
    for i in range(5):
        t = Task(project_id=proj.id, assigned_to=ann_a.id,
                 status="assigned",
                 input_data=json.dumps({
                     "kind": "translation",
                     "text": f"sentence {i}"}),
                 expected_output=(json.dumps({"text": f"oración {i}"})
                                  if i == 0 else None))
        tasks.append(t)
        db.session.add(t)
    db.session.flush()

    subs = [
        Submission(task_id=tasks[0].id, annotator_id=ann_a.id,
                   output_data=json.dumps({"kind": "translation",
                                           "text": "oración 0"}),
                   status="approved", qa_score=90,
                   submitted_at=utc_now(), reviewed_at=utc_now()),
        Submission(task_id=tasks[1].id, annotator_id=ann_b.id,
                   output_data=json.dumps({"kind": "translation",
                                           "text": "oración 1"}),
                   status="approved", qa_score=80,
                   submitted_at=utc_now(), reviewed_at=utc_now()),
        Submission(task_id=tasks[2].id, annotator_id=ann_a.id,
                   output_data=json.dumps({"kind": "translation",
                                           "text": "oración 2"}),
                   status="submitted", submitted_at=utc_now()),
        Submission(task_id=tasks[3].id, annotator_id=ann_b.id,
                   output_data=json.dumps({"kind": "translation",
                                           "text": "oración 3"}),
                   status="rejected", qa_score=30,
                   submitted_at=utc_now(), reviewed_at=utc_now()),
    ]
    db.session.add_all(subs)

    pay_pending = Payment(annotator_id=ann_a.id, amount=money("0.30"),
                          currency="USD", tasks_count=3,
                          period_start=utc_now(), period_end=utc_now(),
                          status="pending")
    pay_paid = Payment(annotator_id=ann_b.id, amount=money("0.10"),
                       currency="USD", tasks_count=1,
                       period_start=utc_now(), period_end=utc_now(),
                       status="paid", payment_method="bank_transfer",
                       paid_at=utc_now())
    db.session.add_all([pay_pending, pay_paid])

    empty_proj = Project(name="Empty Proj", client_name="Client Y",
                         task_type="classification", created_by=admin.id,
                         pay_per_task=money("0.05"),
                         instructions="n/a")
    db.session.add(empty_proj)
    db.session.commit()
    pid, empty_pid = proj.id, empty_proj.id
    pending_id = pay_pending.id
    a_id, b_id = ann_a.id, ann_b.id
    t_ids = [t.id for t in tasks]  # capture before objects expire

adm = app.test_client()
login(adm, "admin@brydl.ng", "admin-pass-1")
ann = app.test_client()
login(ann, "a@brydl.ng", "annotator-pass-1")

# ---- RBAC ---------------------------------------------------------------
print("== RBAC ==")
r = app.test_client().get(f"/admin/projects/{pid}/export")
expect(r.status_code == 302 and "/login" in r.headers["Location"],
       "anon: export page redirects to sign-in")
r = ann.get(f"/admin/projects/{pid}/export")
expect(r.status_code == 403, "annotator: export page is 403")
r = ann.post(f"/admin/projects/{pid}/export", data={"format": "jsonl"})
expect(r.status_code == 403, "annotator: export POST is 403")
r = app.test_client().post("/admin/payments/bank-batch", data={})
expect(r.status_code == 302 and "/login" in r.headers["Location"],
       "anon: bank batch redirects to sign-in")
expect(adm.get("/admin/projects/999999/export").status_code == 404,
       "unknown project: export page 404s")

# ---- options page --------------------------------------------------------
print("== Options page ==")
page = adm.get(f"/admin/projects/{pid}/export").get_data(as_text=True)
expect("Export Proj" in page and "include_gold" in page
       and "Approved only" in page,
       "options page shows project, scope and gold toggle")
projects_page = adm.get("/admin/projects").get_data(as_text=True)
expect(f"/admin/projects/{pid}/export" in projects_page,
       "projects list carries an Export link")

# ---- JSONL export ---------------------------------------------------------
print("== JSONL ==")
def export(fmt="jsonl", scope="approved", gold=None):
    return adm.post(f"/admin/projects/{pid}/export", data={
        "format": fmt, "scope": scope, "csrf_token": get_csrf(
            adm.get(f"/admin/projects/{pid}/export")
            .get_data(as_text=True)),
        **({"include_gold": "1"} if gold else {})})


r = export()
body = r.get_data(as_text=True)
lines = [json.loads(line) for line in body.splitlines() if line.strip()]
expect(r.status_code == 200
       and r.mimetype == "application/x-ndjson",
       "JSONL served with the right content type")
expect("attachment" in r.headers.get("Content-Disposition", "")
       and ".jsonl" in r.headers.get("Content-Disposition", ""),
       "download disposition with .jsonl filename")
expect(len(lines) == 2, "approved-only scope yields exactly 2 rows")
expect(all("gold" not in row for row in lines),
       "gold answers absent by default")
row0 = next(row for row in lines if row["task_id"] == t_ids[0])
expect(row0["is_gold"] is True and row0["input"]["text"] == "sentence 0"
       and row0["output"]["text"] == "oración 0"
       and row0["qa_score"] == 90,
       "input/output parsed as objects, score + gold flag correct")
expect(row0["annotator_id"] == a_id and "name" not in row0,
       "annotators pseudonymous (id only, no names)")

r = export(gold=True)
lines = [json.loads(line)
         for line in r.get_data(as_text=True).splitlines() if line.strip()]
gold_row = next(row for row in lines if row["task_id"] == t_ids[0])
expect(gold_row.get("gold", {}).get("text") == "oración 0",
       "include_gold=1 exposes the answer key on request")

r = export(scope="reviewed")
n_reviewed = len([line for line in r.get_data(as_text=True)
                  .splitlines() if line.strip()])
expect(n_reviewed == 3, "reviewed scope includes rejected (3 rows)")
r = export(scope="all")
n_all = len([line for line in r.get_data(as_text=True)
             .splitlines() if line.strip()])
expect(n_all == 4, "all scope includes submitted (4 rows)")

# ---- CSV export ------------------------------------------------------------
print("== CSV ==")
r = export(fmt="csv", scope="all")
raw = r.get_data()
expect(r.mimetype == "text/csv"
       and "attachment" in r.headers.get("Content-Disposition", "")
       and ".csv" in r.headers.get("Content-Disposition", ""),
       "CSV served as attachment with .csv filename")
reader = csv.reader(io.StringIO(raw.decode("utf-8-sig")))
rows = list(reader)
expect(rows[0] == ["task_id", "status", "qa_score", "annotator_id",
                   "is_gold", "submitted_at_utc", "reviewed_at_utc",
                   "input", "output", "gold", "consensus_status",
                   "consensus_decision"],
       "header row exact")
expect(len(rows) - 1 == 4, "4 data rows for scope=all")
csv_gold_row = next(row for row in rows[1:]
                    if row[0] == str(t_ids[0]))
expect(csv_gold_row[4] == "1" and csv_gold_row[9] == "",
       "is_gold flagged, gold column empty unless requested")
input_obj = json.loads(csv_gold_row[7])
expect(input_obj["text"] == "sentence 0",
       "CSV input cells are compact JSON objects")

# ---- audit + CSRF -----------------------------------------------------------
print("== Audit & CSRF ==")
with app.app_context():
    row = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "project.export",
        AuditLog.entity_id == pid).order_by(AuditLog.id.desc()))
    detail = row.detail if row else ""
expect(row is not None and '"rows": 4' in detail and '"csv"' in detail,
       "project.export audited with format + row count")
expect(row is not None and "oración" not in detail,
       "audit records the event, never the data")
r = adm.post(f"/admin/projects/{pid}/export", data={"format": "csv"})
expect(r.status_code == 302, "export without CSRF refused")
with app.app_context():
    n_exports = db.session.scalar(db.select(db.func.count(AuditLog.id))
                                  .where(AuditLog.action ==
                                         "project.export"))
expect(n_exports == 5, "refused export added no audit row (5 successes)")

# ---- empty project ------------------------------------------------------------
print("== Empty project ==")
r = adm.post(f"/admin/projects/{empty_pid}/export", data={
    "format": "jsonl", "scope": "all", "csrf_token": get_csrf(
        adm.get(f"/admin/projects/{empty_pid}/export")
        .get_data(as_text=True))})
expect(r.status_code == 200 and r.get_data(as_text=True).strip() == "",
       "empty project exports a valid empty JSONL file")
r = adm.post(f"/admin/projects/{empty_pid}/export", data={
    "format": "csv", "scope": "all", "csrf_token": get_csrf(
        adm.get(f"/admin/projects/{empty_pid}/export")
        .get_data(as_text=True))})
expect(r.get_data(as_text=True).count("\r\n") == 1,
       "empty project CSV is header-only")

# ---- bank batch -----------------------------------------------------------------
print("== Bank batch ==")
r = adm.post("/admin/payments/bank-batch", data={
    "csrf_token": get_csrf(adm.get("/admin/payments")
                           .get_data(as_text=True))})
raw = r.get_data(as_text=True).lstrip("\ufeff")
rows = list(csv.reader(io.StringIO(raw)))
expect(r.status_code == 200 and r.mimetype == "text/csv"
       and "attachment" in r.headers.get("Content-Disposition", "")
       and ".csv" in r.headers.get("Content-Disposition", ""),
       "bank batch served as a CSV attachment")
expect(rows[0] == ["reference", "recipient_name", "recipient_email",
                   "recipient_bank", "recipient_account_number",
                   "recipient_account_name",
                   "amount_ngn", "amount_usd", "fx_rate", "tasks",
                   "period", "narration"],
       "bank batch header exact (incl. payout account columns)")
expect(len(rows) == 2, "only PENDING payments in the batch (1 row)")
batch_row = rows[1]
expect(batch_row[0] == f"BRYDL-PAY-{pending_id}"
       and batch_row[1] == "Tunde A" and batch_row[2] == "a@brydl.ng",
       "reference + recipient details correct")
expect(batch_row[3] == "" and batch_row[4] == "",
       "blank bank columns when the annotator has no details on file")
expect(Decimal(batch_row[6]) == Decimal("480.00")
       and Decimal(batch_row[7]) == Decimal("0.30")
       and Decimal(batch_row[8]) == Decimal("1600"),
       "NGN exact at the configured rate (0.30 USD -> 480.00 NGN)")

r = adm.post("/admin/payments/bank-batch", data={})
expect(r.status_code == 302, "bank batch without CSRF refused")
with app.app_context():
    row = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "payment.export_batch")
        .order_by(AuditLog.id.desc()))
expect(row is not None and '"count": 1' in row.detail
       and "Tunde" not in row.detail,
       "payment.export_batch audited (count only, no personal data)")

# ---- hygiene -----------------------------------------------------------------
print("== Hygiene ==")
for template in ("admin/project_export.html", "admin/projects.html",
                 "admin/payments.html"):
    src = open(os.path.join(ROOT, "templates", template)).read()
    assert not re.search(r"\son[a-z]+=\"", src), template
expect(True, "no inline event handlers in touched templates")
payments_page = adm.get("/admin/payments").get_data(as_text=True)
expect("Bank batch CSV" in payments_page,
       "payments page carries the bank-batch button")
expect("EXPORT_YIELD_PER" in open(
    os.path.join(ROOT, "config.py")).read(),
    "export batch size is a config knob (no magic numbers)")

shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)
print()
if FAILURES:
    print(f"EXPORT TESTS FAILED ({len(FAILURES)}):")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("ALL EXPORT TESTS PASSED")
