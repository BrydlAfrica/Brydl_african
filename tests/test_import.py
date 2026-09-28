import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""CSV task-import suite: device upload, validation, editable preview.

Covers the security standards end to end: role + CSRF gates, extension
and size caps, binary refusal, header/row validation identical to the
paste flow, duplicate detection (in-file and against the project),
preview-then-confirm with server-side re-validation (a tampered or
edited preview can never smuggle bad rows in), atomic inserts, audit
coverage, the template download and the CSV formula-injection guard on
exports.
"""
import io
import re
import shutil
import sys

from decimal import Decimal

from app import create_app
from extensions import db
from models import AuditLog, Project, Task, User
from routes.admin import IMPORT_MAX_BYTES, IMPORT_MAX_ROWS, _csv_safe_cell

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


def login(client, email, password):
    """POST the sign-in form."""
    r = client.get("/login")
    tk = get_csrf(r.get_data(as_text=True))
    return client.post("/login", data={"email": email,
                                       "password": password,
                                       "csrf_token": tk})


def flash_of(response, client):
    """Follow a redirect and return the flashed segment of the page."""
    return client.get(response.headers["Location"]).get_data(as_text=True)


def csv_bytes(*lines):
    """Build an import file body from raw lines."""
    return ("\n".join(lines) + "\n").encode("utf-8")


def upload(admin, project_id, body, filename="tasks.csv"):
    """POST the import form as a multipart file upload."""
    return admin.post(
        "/admin/tasks/import",
        data={"project": str(project_id), "csv_file": (io.BytesIO(body),
                                                       filename),
              "csrf_token": get_csrf(admin.get("/admin/tasks").get_data(
                  as_text=True))},
        content_type="multipart/form-data",
        follow_redirects=False,
    )


def count_tasks(project_id):
    """Number of tasks currently in a project."""
    with app.app_context():
        return db.session.scalar(db.select(db.func.count(Task.id)).where(
            Task.project_id == project_id))


shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)

# ---- seed: admin + annotator + active project with one existing task ----
with app.app_context():
    admin = User(name="Ada Admin", email="admin@brydl.ng", role="admin")
    admin.set_password("admin-pass-1")
    ann = User(name="Tunde A", email="a@brydl.ng", role="annotator")
    ann.set_password("annotator-pass-1")
    db.session.add_all([admin, ann])
    db.session.flush()  # ids first: projects.created_by is NOT NULL
    project = Project(name="Import target", client_name="Client",
                      task_type="classification",
                      pay_per_task=Decimal("0.05"),
                      instructions="Label the sentiment.",
                      created_by=admin.id)
    db.session.add(project)
    db.session.flush()
    seed_task = Task(project_id=project.id, input_data="Already here")
    db.session.add(seed_task)
    db.session.commit()
    project_id, ann_id = project.id, ann.id

# ---- 1. access control ----------------------------------------------------
print("== 1. access control ==")
r = app.test_client().get("/admin/tasks/import")
expect(r.status_code == 302 and "/login" in r.headers["Location"],
       "anonymous: import redirects to sign-in")
r = app.test_client().post("/admin/tasks/import", data={})
expect(r.status_code == 302, "anonymous POST: redirected, not 500")
ann_c = app.test_client()
login(ann_c, "a@brydl.ng", "annotator-pass-1")
r = ann_c.post("/admin/tasks/import", data={"project": str(project_id),
                                            "csv_file": (
                                                io.BytesIO(b"text\nx\n"),
                                                "x.csv")},
               content_type="multipart/form-data")
expect(r.status_code == 403, "annotator: import is 403 Forbidden")

# ---- 2. CSRF --------------------------------------------------------------
print("== 2. CSRF ==")
admin = app.test_client()
login(admin, "admin@brydl.ng", "admin-pass-1")
r = admin.post("/admin/tasks/import", data={
    "project": str(project_id),
    "csv_file": (io.BytesIO(b"text\nhello\n"), "t.csv")},
    content_type="multipart/form-data", follow_redirects=True)
expect("Security check failed" in r.get_data(as_text=True),
       "import without CSRF token is refused")
expect(count_tasks(project_id) == 1, "no row written without CSRF")

# ---- 3. template download -------------------------------------------------
print("== 3. template download ==")
r = admin.get("/admin/tasks/import/template")
expect(r.status_code == 200 and "text/csv" in r.mimetype
       and r.get_data(as_text=True).lstrip("\ufeff").startswith(
           "text,gold"),
       "CSV template downloads with the documented header")

# ---- 4. file-type + size + encoding gates --------------------------------
print("== 4. file gates ==")
r = upload(admin, project_id, b"text\nhello\n", filename="tasks.txt")
expect("Choose a .csv file" in flash_of(r, admin),
       "non-.csv extension refused")
r = upload(admin, project_id, b"MZ\x90\x00binarygarbage\xff\xfe")
expect("not a UTF-8 text/CSV" in flash_of(r, admin),
       "binary content refused (UTF-8 decode)")
r = upload(admin, project_id,
           b"text\n" + b"x" * (IMPORT_MAX_BYTES + 10))
expect("File too large" in flash_of(r, admin),
       "oversize file refused at the byte cap")
r = upload(admin, project_id, b"hello,world\n")
expect("First column" in flash_of(r, admin),
       "missing 'text' header refused")
r = upload(admin, project_id, b"text,gold,extra\nhello,,\n")
expect("Too many columns" in flash_of(r, admin),
       "unexpected extra column refused")
r = upload(admin, project_id, b"text,notes\nhello,world\n")
expect("Second column must be named" in flash_of(r, admin),
       "misnamed second column refused")
expect(count_tasks(project_id) == 1, "rejected files wrote nothing")

# ---- 5. preview: valid rows + row-level problems + duplicates ------------
print("== 5. preview + validation ==")
body = csv_bytes(
    "text,gold",
    "This movie was great,positive",
    "No gold row here",
    "Already here,positive",              # duplicate of project task
    "This movie was great,negative",      # duplicate inside the file
    ",positive",                          # empty text (gold present)
    "x" * 10_001,                         # over-length text
)
r = upload(admin, project_id, body)
page = r.get_data(as_text=True)
expect(r.status_code == 200 and "Import preview" in page,
       "preview renders on a valid structure")
expect("Duplicate inside this file." in page,
       "in-file duplicate flagged")
expect("already in the project" in page,
       "duplicate against existing project task flagged")
expect("Task text is empty." in page, "empty-text row flagged")
expect("exceeds 10000 characters" in page, "over-length row flagged")
expect(page.count(" checked>") == 2,
       "exactly the 2 clean rows come pre-ticked")
expect(count_tasks(project_id) == 1, "preview writes nothing to the DB")

# ---- 6. confirm: happy path ----------------------------------------------
print("== 6. confirm (happy path) ==")
r = admin.post("/admin/tasks/import/confirm", data={
    "project": str(project_id), "n_rows": "6",
    "row_include_0": "y", "row_text_0": "This movie was great",
    "row_gold_0": "positive",
    "row_include_1": "y", "row_text_1": "No gold row here",
    "row_gold_1": "",
    "csrf_token": get_csrf(admin.get("/admin/tasks").get_data(
        as_text=True))}, follow_redirects=True)
body_text = r.get_data(as_text=True)
expect("Imported 2 tasks into" in body_text, "confirm flashes the count")
expect(count_tasks(project_id) == 3, "2 new rows in the DB (3 total)")
with app.app_context():
    gold_saved = db.session.scalar(db.select(Task.expected_output).where(
        Task.input_data == "This movie was great"))
    audit_ok = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "task.import",
        AuditLog.entity_id == project_id)) is not None
expect(gold_saved == "positive", "gold answer stored with the task")
expect(audit_ok, "task.import audited (counts only)")

# ---- 7. confirm: edit-to-fix flow (the whole point) ----------------------
print("== 7. preview edits survive to the DB ==")
r = upload(admin, project_id, csv_bytes("text,gold", ",neutral", ""))
page = r.get_data(as_text=True)
expect("Task text is empty." in page, "bad row shows in preview")
# admin fixes the empty row in the preview table and ticks it
r = admin.post("/admin/tasks/import/confirm", data={
    "project": str(project_id), "n_rows": "1",
    "row_include_0": "y", "row_text_0": "Fixed during preview",
    "row_gold_0": "neutral",
    "csrf_token": get_csrf(admin.get("/admin/tasks").get_data(
        as_text=True))}, follow_redirects=True)
expect("Imported 1 tasks into" in r.get_data(as_text=True),
       "edited row accepted at confirm")
with app.app_context():
    fixed = db.session.scalar(db.select(Task.expected_output).where(
        Task.input_data == "Fixed during preview"))
expect(fixed == "neutral", "edited text + gold saved verbatim")

# ---- 8. confirm: tampered / re-validated --------------------------------
print("== 8. confirm re-validates server-side ==")
before = count_tasks(project_id)
r = admin.post("/admin/tasks/import/confirm", data={
    "project": str(project_id), "n_rows": "1",
    "row_include_0": "y", "row_text_0": "x" * 10_001,
    "csrf_token": get_csrf(admin.get("/admin/tasks").get_data(
        as_text=True))}, follow_redirects=True)
body_text = r.get_data(as_text=True)
expect("exceeds 10000" in body_text and "Nothing was saved" in body_text,
       "oversize row at confirm refused atomically")
r = admin.post("/admin/tasks/import/confirm", data={
    "project": str(project_id), "n_rows": "999999",
    "csrf_token": get_csrf(admin.get("/admin/tasks").get_data(
        as_text=True))}, follow_redirects=True)
expect("Malformed import batch" in r.get_data(as_text=True),
       "tampered n_rows refused")
r = admin.post("/admin/tasks/import/confirm", data={
    "project": str(project_id), "n_rows": "1",
    "csrf_token": get_csrf(admin.get("/admin/tasks").get_data(
        as_text=True))}, follow_redirects=True)
expect("no rows were ticked" in r.get_data(as_text=True),
       "confirm with nothing ticked saves nothing")
expect(count_tasks(project_id) == before, "DB unchanged by tampering")

# ---- 9. row cap ----------------------------------------------------------
print("== 9. row cap ==")
r = upload(admin, project_id,
           csv_bytes("text", *[f"row-{i}" for i in
                               range(IMPORT_MAX_ROWS + 1)]))
expect("Too many rows" in flash_of(r, admin), "over-cap file refused")

# ---- 10. export formula guard --------------------------------------------
print("== 10. CSV export formula guard ==")
expect(_csv_safe_cell("=cmd|' /C calc'!A0") == "'=cmd|' /C calc'!A0",
       "= cell neutralized")
expect(_csv_safe_cell("+1+1") == "'+1+1"
       and _csv_safe_cell("-2") == "'-2"
       and _csv_safe_cell("@cmd") == "'@cmd",
       "+, -, @ cells neutralized")
expect(_csv_safe_cell("normal text") == "normal text"
       and _csv_safe_cell(4.2) == 4.2,
       "normal text and numbers untouched")

shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)
print()
if FAILURES:
    print(f"IMPORT TESTS FAILED ({len(FAILURES)}):")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("ALL CSV IMPORT TESTS PASSED")
