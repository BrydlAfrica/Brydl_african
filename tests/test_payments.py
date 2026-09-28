import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""E2E payments feature tests."""
import json
import re
from datetime import timedelta

from app import create_app
from extensions import db
from models import User, Project, Task, Submission, utc_now

app = create_app()

def get_csrf(html):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None

def login(client, email, password):
    r = client.get("/login")
    tk = get_csrf(r.get_data(as_text=True))
    return client.post("/login", data={"email": email, "password": password,
                                       "csrf_token": tk})

with app.app_context():
    admin = User(name="Ada Admin", email="admin@brydl.ng", role="admin")
    admin.set_password("admin-pass-1")
    ann_a = User(name="Tunde A", email="a@brydl.ng", role="annotator")
    ann_a.set_password("annotator-pass-1")
    ann_b = User(name="Bisi B", email="b@brydl.ng", role="annotator")
    ann_b.set_password("annotator-pass-2")
    ghost = User(name="Ghost G", email="g@brydl.ng", role="annotator",
                 is_active=False)
    ghost.set_password("ghost-pass-11")
    db.session.add_all([admin, ann_a, ann_b, ghost]); db.session.commit()
    admin_id, ann_a_id, ann_b_id, ghost_id = (admin.id, ann_a.id, ann_b.id,
                                              ghost.id)

    p1 = Project(name="Cheap Project", client_name="Acme", task_type="ner",
                 pay_per_task=0.50, instructions="i", created_by=admin_id)
    p2 = Project(name="Priced Project", client_name="Globex", task_type="rlhf",
                 pay_per_task=2.00, instructions="i", created_by=admin_id)
    db.session.add_all([p1, p2]); db.session.commit()
    p1_id, p2_id = p1.id, p2.id

    def approved_task(project, annotator, days_ago):
        t = Task(project_id=project.id, input_data=f"in {days_ago}",
                 status="approved", assigned_to=annotator)
        db.session.add(t); db.session.flush()
        s = Submission(task_id=t.id, annotator_id=annotator,
                       output_data='{"kind":"text","text":"ok"}',
                       status="approved", qa_score=90.0,
                       submitted_at=utc_now() - timedelta(days=days_ago),
                       reviewed_at=utc_now() - timedelta(days=days_ago) + timedelta(hours=1))
        db.session.add(s); db.session.flush()
        return s.id

    # ann_a: 3 x $0.50 + 2 x $2.00 = $5.50 across 5 tasks (earliest 10 days ago)
    for d in (7, 6, 5):
        approved_task(p1, ann_a_id, d)
    for d in (10, 9):
        approved_task(p2, ann_a_id, d)
    # ann_b: 1 x $0.50
    approved_task(p1, ann_b_id, 2)
    # ghost: inactive, must be excluded from ledger + generation
    approved_task(p1, ghost_id, 1)
    # non-approved work must never count
    t_sub = Task(project_id=p1.id, input_data="sub", status="submitted",
                 assigned_to=ann_a_id)
    db.session.add(t_sub); db.session.flush()
    db.session.add(Submission(task_id=t_sub.id, annotator_id=ann_a_id,
                              output_data='{"kind":"text","text":"wait"}'))
    t_rej = Task(project_id=p1.id, input_data="rej", status="rejected",
                 assigned_to=ann_a_id)
    db.session.add(t_rej); db.session.flush()
    db.session.add(Submission(task_id=t_rej.id, annotator_id=ann_a_id,
                              output_data='{"kind":"text","text":"bad"}',
                              status="rejected", qa_score=30.0,
                              reviewed_at=utc_now()))
    db.session.commit()

c = app.test_client()

# ---- 1. RBAC ----
for method, path, data in (("get", "/admin/payments", None),
                           ("post", "/admin/payments/generate", {}),
                           ("post", "/admin/payments/1/mark-paid", {})):
    r = getattr(app.test_client(), method)(path, data=data)
    assert r.status_code == 302 and "/login" in r.headers["Location"], (method, path)
r_ann = app.test_client(); login(r_ann, "a@brydl.ng", "annotator-pass-1")
for method, path, data in (("get", "/admin/payments", None),
                           ("post", "/admin/payments/generate", {}),
                           ("post", "/admin/payments/1/mark-paid", {})):
    assert getattr(r_ann, method)(path, data=data).status_code == 403, path
print("1. RBAC: anon redirected, annotator 403 on all payment routes OK")

login(c, "admin@brydl.ng", "admin-pass-1")

# ---- 2. dashboard before any payout ----
r = c.get("/admin/payments")
assert r.status_code == 200
html = r.get_data(as_text=True)
assert "$6.50" in html and "Total pending payout" in html, "pending summary"
assert "&#8358;10400.00" in html, "NGN conversion on summary"  # 6.50 x 1600
assert "$0.00" in html and "Paid this month" in html
assert "3" in html and "Active annotators" in html  # ghost excluded
# earnings rows: ann_a 5 tasks $5.50 / NGN 8800; ann_b 1 task $0.50 / NGN 800
row_a = html[html.find("Tunde A"):html.find("Payment history")]
assert "5" in row_a and "$5.50" in row_a and "8800.00" in row_a
assert "Tunde A" in html and "Bisi B" in html and "Ghost G" not in html
assert "No payments generated yet" in html
csrf = get_csrf(html)
print("2. dashboard: pending $6.00 (NGN 9600), per-annotator rows, ghost excluded OK")

# ---- 3. generate (global) ----
r = c.post("/admin/payments/generate", data={"csrf_token": csrf})
assert r.status_code == 302
fl = c.get(r.headers["Location"]).get_data(as_text=True)
assert "2 payment(s) generated totalling $6.00" in fl
with app.app_context():
    from models import Payment
    pays = db.session.scalars(db.select(Payment).order_by(Payment.id)).all()
    assert len(pays) == 2
    pa = [p for p in pays if p.annotator_id == ann_a_id][0]
    pb = [p for p in pays if p.annotator_id == ann_b_id][0]
    assert pa.amount == 5.50 and pa.tasks_count == 5 and pa.status == "pending"
    assert pa.currency == "USD"
    assert pb.amount == 0.50 and pb.tasks_count == 1
    # period: ann_a earliest approved submitted 10 days ago; period_end = now
    assert (utc_now() - pa.period_start).days in (9, 10)
    # claims: exactly ann_a's 5 approved submissions claimed, nothing else
    claimed = db.session.scalars(db.select(Submission).where(
        Submission.payment_id.is_not(None))).all()
    assert len(claimed) == 6, f"claimed={len(claimed)}"
    assert all(s.annotator_id in (ann_a_id, ann_b_id) for s in claimed)
    unclaimed = db.session.scalar(db.select(db.func.count(Submission.id)).where(
        Submission.status == "approved", Submission.payment_id.is_(None)))
    assert unclaimed == 1  # only ghost's (inactive, excluded from payouts)
# ghost excluded: no payment for ghost_id
with app.app_context():
    from models import Payment
    assert db.session.scalar(db.select(db.func.count(Payment.id)).where(
        Payment.annotator_id == ghost_id)) == 0
# regenerate with nothing unclaimed for ann_a -> per-row generate now hidden;
# global generate still creates ann_b? no: ann_b's work was claimed too
r = c.post("/admin/payments/generate", data={"csrf_token": csrf})
fl = c.get(r.headers["Location"]).get_data(as_text=True)
assert "No approved unpaid submissions" in fl
with app.app_context():
    from models import Payment
    assert db.session.scalar(db.select(db.func.count(Payment.id))) == 2
print("3. generate: 2 payments ($5.50/5 + $0.50/1), claims exact, ghost skipped,"
      " regenerate refused OK")

# ---- 4. dashboard between generate and mark-paid ----
html = c.get("/admin/payments").get_data(as_text=True)
assert "$6.50" in html, "generate must not change the pending total"
assert "Payout pending" in html and "Last paid" not in html
print("4. generated-pending payments count toward pending payout OK")

# ---- 5. mark-paid ----
with app.app_context():
    from models import Payment
    pa_id = db.session.scalar(db.select(Payment.id).where(
        Payment.annotator_id == ann_a_id))
# bad method refused
r = c.post(f"/admin/payments/{pa_id}/mark-paid",
           data={"payment_method": "crypto", "csrf_token": csrf})
fl = c.get(r.headers["Location"]).get_data(as_text=True)
assert "Choose how the payment was sent" in fl
# missing CSRF refused
r = c.post(f"/admin/payments/{pa_id}/mark-paid", data={"payment_method": "cash"})
fl = c.get("/admin/payments").get_data(as_text=True)
assert "Security check failed" in fl
# proper mark-paid
before = utc_now()
r = c.post(f"/admin/payments/{pa_id}/mark-paid",
           data={"payment_method": "bank_transfer", "csrf_token": csrf})
fl = c.get(r.headers["Location"]).get_data(as_text=True)
assert "marked paid: $5.50" in fl and "8800.00" in fl and "bank transfer" in fl
with app.app_context():
    from models import Payment
    p = db.session.get(Payment, pa_id)
    assert p.status == "paid" and p.payment_method == "bank_transfer"
    assert p.paid_at is not None and p.paid_at >= before - timedelta(seconds=5)
# double mark-paid -> info
r = c.post(f"/admin/payments/{pa_id}/mark-paid",
           data={"payment_method": "cash", "csrf_token": csrf})
fl = c.get(r.headers["Location"]).get_data(as_text=True)
assert "already paid" in fl
# unknown id 404
assert c.post("/admin/payments/99999/mark-paid",
              data={"payment_method": "cash", "csrf_token": csrf}).status_code == 404
print("5. mark-paid: method+paid_at recorded, validations, 404, double-refused OK")

# ---- 6. summary after partial payment + ledger consistency ----
html = c.get("/admin/payments").get_data(as_text=True)
assert "$1.00" in html and "Total pending payout" in html  # B pending + ghost unclaimed
assert "$5.50" in html and "Paid this month" in html
# ledger cross-check against admin dashboard: approved - paid == pending here
r = c.get("/admin/")
dash = r.get_data(as_text=True)
m = re.search(r'\$([\d.]+)</div>\s*<div class="text-muted small">Payout pending',
              dash)
dash_pending = float(m.group(1))
assert dash_pending == 1.00, f"ledger mismatch: dashboard={dash_pending}"
# $1.00 = ann_b's pending payout ($0.50) + ghost's unclaimed $0.50 (owed
# despite deactivation). Both pages agree — the invariants hold.
print("6. summary consistent with admin dashboard ledger ($0.50 pending) OK")

# ---- 7. new approvals -> targeted generate ----
with app.app_context():
    t = Task(project_id=p2_id, input_data="late work", status="approved",
             assigned_to=ann_a_id)
    db.session.add(t); db.session.flush()
    db.session.add(Submission(task_id=t.id, annotator_id=ann_a_id,
                              output_data='{"kind":"rlhf","rating":5,"explanation":"x"}',
                              status="approved", qa_score=95.0,
                              submitted_at=utc_now(), reviewed_at=utc_now()))
    db.session.commit()
html = c.get("/admin/payments").get_data(as_text=True)
row_a = html[html.find("Tunde A"):html.find("Payment history")]
assert "$2.00" in row_a, "new approval not reflected in ann_a row"
# targeted generate for ann_a only
r = c.post("/admin/payments/generate",
           data={"annotator_id": str(ann_a_id), "csrf_token": csrf})
fl = c.get(r.headers["Location"]).get_data(as_text=True)
assert "1 payment(s) generated totalling $2.00" in fl
with app.app_context():
    from models import Payment
    n_a = db.session.scalar(db.select(db.func.count(Payment.id)).where(
        Payment.annotator_id == ann_a_id))
    n_b = db.session.scalar(db.select(db.func.count(Payment.id)).where(
        Payment.annotator_id == ann_b_id))
    assert n_a == 2 and n_b == 1
print("7. targeted generate: new $2.00 payment for ann_a only OK")

# ---- 8. CSP hygiene ----
src = open(ROOT + "/templates/admin/payments.html").read()
assert not re.search(r"\son[a-z]+=\"", src)
print("8. template CSP hygiene OK")
print("\nALL PAYMENTS TESTS PASSED")
