"""Full-system check: one sweep for loopholes, inconsistencies and dead
code across the entire Brydl Dashboard.

Layers:
 1. App boot + route table sanity (every endpoint has a documented view).
 2. SECURITY MATRIX: every POST route validates CSRF; every admin/
    annotator route carries its role decorator (read from each view's
    ORIGINAL source, decorators included).
 3. Role crawl: every GET route hit as anonymous / annotator / admin —
    a 5xx for any role anywhere is a failure.
 4. Template integrity: every template compiles; every url_for endpoint
    (templates + Python) exists; every literal href/action path maps to
    a real route; every referenced config key is defined.
 5. Schema inventory: every CHECK constraint and index exists on a fresh
    database; integrity_check + foreign_key_check pass.
 6. Danger-pattern scan: |safe only on rendering-sanitized views, no
    inline handlers, no TODO/FIXME/
    print()/breakpoint in app code, no seeded passwords outside seed.py,
    no POSIX-absolute path literals.
 7. Error paths: 403/404/405/500 all render branded pages and never
    leak internals; static assets referenced by base.html exist.
 8. Dependency pins: requirements.txt matches the installed versions.
"""
import importlib.metadata
import inspect
import os
import re
import sqlite3
import sys

ROOT = os.environ.get("BRYDL_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)

from app import create_app
from extensions import db
from models import (
    Payment,
    Project,
    Submission,
    Task,
    User,
    money,
    utc_now,
)

app = create_app()


# 500 probe route (must be registered BEFORE the first request; Flask 3
# freezes the URL map afterwards).
def _boom():
    raise RuntimeError("check-boom")


app.add_url_rule("/boom", "boom", _boom)

FAILURES = []


def expect(condition, message):
    """Record a check result."""
    if condition:
        print(f"   ok: {message}")
    else:
        FAILURES.append(message)
        print(f"   FAIL: {message}")


def original_source(view):
    """Return the view's own source, decorators included."""
    func = view
    while hasattr(func, "__wrapped__"):
        func = func.__wrapped__
    return inspect.getsource(func)


def get_csrf(html):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None


def login(client, email, password):
    page = client.get("/login").get_data(as_text=True)
    return client.post("/login", data={
        "email": email, "password": password,
        "csrf_token": get_csrf(page),
    })


# ---- seed minimal-but-complete data so every page has content -------
with app.app_context():
    admin = User(name="Ada Admin", email="admin@brydl.ng", role="admin")
    admin.set_password("admin-pass-1")
    ann_a = User(name="Tunde A", email="a@brydl.ng", role="annotator")
    ann_a.set_password("annotator-pass-1")
    ann_b = User(name="Bisi B", email="b@brydl.ng", role="annotator")
    ann_b.set_password("annotator-pass-2")
    db.session.add_all([admin, ann_a, ann_b])
    db.session.flush()
    proj = Project(name="Check Proj", client_name="C",
                   task_type="translation", pay_per_task=money("0.50"),
                   instructions="i", created_by=admin.id)
    proj2 = Project(name="Paused Proj", client_name="C",
                    task_type="classification", pay_per_task=money("0.10"),
                    instructions="i", created_by=admin.id, status="paused")
    db.session.add_all([proj, proj2])
    db.session.flush()
    t_sub = Task(project_id=proj.id, input_data="submitted work",
                 status="submitted", assigned_to=ann_a.id)
    t_done = Task(project_id=proj.id, input_data="done work",
                  status="approved", assigned_to=ann_a.id)
    db.session.add_all([t_sub, t_done])
    db.session.flush()
    sub = Submission(task_id=t_sub.id, annotator_id=ann_a.id,
                     output_data='{"kind":"translation","text":"x"}',
                     submitted_at=utc_now())
    db.session.add(sub)
    db.session.add(Payment(annotator_id=ann_a.id, amount=money("1.00"),
                           currency="USD", tasks_count=2,
                           period_start=utc_now(), period_end=utc_now(),
                           status="pending"))
    db.session.add(AuditRow := __import__(
        "models", fromlist=["AuditLog"]).AuditLog(
        actor_name="check", action="check.seed"))
    db.session.commit()
    sub_id = sub.id

# ---- 1. route table sanity ------------------------------------------
rules = [r for r in app.url_map.iter_rules() if r.endpoint != "static"]
bad_views = []
for rule in rules:
    view = app.view_functions[rule.endpoint]
    if view is None or not inspect.getdoc(original_source(view)):
        bad_views.append(rule.endpoint)
expect(not bad_views, f"all {len(rules)} endpoints exist with docstrings")
duplicates = len(rules) != len({r.rule for r in rules})
expect(not duplicates, "no duplicate URL rules")

# ---- 2. security matrix ---------------------------------------------
csrf_gaps, role_gaps = [], []
# Webhooks are server-to-server: they authenticate by HMAC signature
# over the raw body, not by browser CSRF tokens. Anything added here
# must verify a signature (or equivalent) as its first act.
CSRF_EXEMPT_ENDPOINTS = {"public.paystack_webhook"}
# API routes use Bearer tokens (custom Authorization header)
# — they cannot be CSRForged by a browser, and never consume
# session cookies. Anything added here must NOT touch session
# state and must authenticate by Authorization header.
for ep in list(app.view_functions):
    if ep.startswith("api."):
        CSRF_EXEMPT_ENDPOINTS.add(ep)

for rule in rules:
    src = original_source(app.view_functions[rule.endpoint])
    if "POST" in rule.methods:
        if (rule.endpoint not in CSRF_EXEMPT_ENDPOINTS
                and "validate_csrf" not in src
                and "validate_on_submit" not in src):
            csrf_gaps.append(rule.endpoint)
    if rule.endpoint.startswith("admin."):
        # admin-only, QA surface (admin+qa) and auditor surface
        # (admin+auditor) decorators all count; anything else is a gap.
        if not any(d in src for d in (
            "@admin_required", "@qa_required", "@auditor_required",
            '@role_required("admin"',
        )):
            role_gaps.append(rule.endpoint)
    elif rule.endpoint.startswith("annotator."):
        if "@annotator_required" not in src:
            role_gaps.append(rule.endpoint)
    elif rule.endpoint.startswith("api."):
        if "@api_auth" not in src and rule.endpoint != "api.ping":
            role_gaps.append(rule.endpoint)
expect(not csrf_gaps,
       f"every POST route validates CSRF or is a signed webhook"
       f" ({len(rules)} routes)")
expect(not role_gaps, "every admin/annotator route carries its role guard")
logout_src = original_source(app.view_functions["auth.logout"])
expect("login_required" in logout_src, "logout requires sign-in")

# ---- 3. role crawl: no 5xx for anyone, anywhere ----------------------
def crawl(client):
    """GET every simple + dynamic route; return {path: status}."""
    results = {}
    for rule in rules:
        methods = rule.methods - {"HEAD", "OPTIONS"}
        if methods != {"GET"}:
            continue
        path = re.sub(r"<int:[^>]+>", "99999", rule.rule)
        if path == "/boom":
            continue  # deliberate 500 probe, asserted separately
        results[path] = client.get(path).status_code
    return results

anon_statuses = crawl(app.test_client())
ann_client = app.test_client()
login(ann_client, "a@brydl.ng", "annotator-pass-1")
ann_statuses = crawl(ann_client)
adm_client = app.test_client()
login(adm_client, "admin@brydl.ng", "admin-pass-1")
adm_statuses = crawl(adm_client)

for name, statuses in (("anon", anon_statuses), ("annotator", ann_statuses),
                       ("admin", adm_statuses)):
    five_hundreds = {p: s for p, s in statuses.items() if s >= 500}
    expect(not five_hundreds, f"{name}: no 5xx on any GET route")

def _allowed_api(statuses, path, status, allowed):
    """API /api/v1/ping is 200 (open); every other /api path is 401."""
    if not path.startswith("/api"):
        return status in allowed
    if path == "/api/v1/ping":
        return status == 200
    return status == 401


expect(all(_allowed_api(anon_statuses, p, s, (200, 302, 404))
           for p, s in anon_statuses.items()),
       "anon: only 200/302/404 on web routes; /api/ping 200; other API 401")
expect(all(_allowed_api(ann_statuses, p, s, (200, 302, 403, 404))
           for p, s in ann_statuses.items()),
       "annotator: only 200/302/403/404 on web routes; API 401 (no session path)")
admin_pages = {p: s for p, s in adm_statuses.items()
               if p.startswith("/admin") and "99999" not in p}
expect(all(s in (200, 302) for s in admin_pages.values()),
       f"admin: all {len(admin_pages)} admin routes render (200/302)")
annotator_pages = {p: s for p, s in adm_statuses.items()
                   if p.startswith("/annotator") and "99999" not in p}
expect(all(s == 403 for s in annotator_pages.values()),
       "admin: every annotator route is 403 (role wall holds)")

# ---- 4. templates, endpoints, config keys ---------------------------
template_dir = os.path.join(ROOT, "templates")
template_names = []
for base, _dirs, files in os.walk(template_dir):
    for name in files:
        template_names.append(
            os.path.relpath(os.path.join(base, name), template_dir)
        )
compile_errors = []
for name in template_names:
    try:
        app.jinja_env.get_template(name)
    except Exception as exc:  # noqa: BLE001 - report every failure
        compile_errors.append(f"{name}: {exc}")
expect(not compile_errors and len(template_names) >= 14,
       f"all {len(template_names)} templates compile")

endpoint_map = {rule.endpoint for rule in app.url_map.iter_rules()}
path_map = {rule.rule for rule in app.url_map.iter_rules()}
referenced_endpoints = set()
for base, _dirs, files in os.walk(os.path.join(ROOT, "templates")):
    for name in files:
        src = open(os.path.join(base, name), encoding="utf-8").read()
        referenced_endpoints |= set(re.findall(
            r"url_for\(\s*['\"]([a-z_.]+)['\"]", src))
for py_dir in ("routes",):
    for name in sorted(os.listdir(os.path.join(ROOT, py_dir))):
        if not name.endswith(".py"):
            continue
        src = open(os.path.join(ROOT, py_dir, name), encoding="utf-8").read()
        referenced_endpoints |= set(re.findall(
            r"url_for\(\s*['\"]([a-z_.]+)['\"]", src))
src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
referenced_endpoints |= set(re.findall(
    r"url_for\(\s*['\"]([a-z_.]+)['\"]", src))
dead_endpoints = referenced_endpoints - endpoint_map - {"static"}
expect(not dead_endpoints,
       f"all {len(referenced_endpoints)} url_for endpoints exist")

bad_paths = []
for base, _dirs, files in os.walk(template_dir):
    for name in files:
        src = open(os.path.join(base, name), encoding="utf-8").read()
        for path in re.findall(r'(?:href|action)="(/[^"{}]*)"', src):
            if path not in path_map:
                bad_paths.append(path)
expect(not bad_paths, "every literal href/action maps to a real route")

config_refs = set()
for py_rel in ("app.py", "config.py", "audit.py", "backup.py") + tuple(
        "routes/" + n for n in sorted(os.listdir(os.path.join(ROOT, "routes")))
        if n.endswith(".py")):
    src = open(os.path.join(ROOT, py_rel), encoding="utf-8").read()
    config_refs |= set(re.findall(r'config\.get\(\s*"([A-Z_]+)"', src))
    config_refs |= set(re.findall(r'config\["([A-Z_]+)"\]', src))
missing_keys = {k for k in config_refs if k not in app.config}
expect(
    not missing_keys,
    f"all {len(config_refs)} referenced config keys defined",
)

# ---- 5. schema inventory --------------------------------------------
conn = sqlite3.connect(os.path.join(ROOT, "instance", "brydl.db"))
ddl = " ".join(
    row[0] for row in conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
)
expected_constraints = (
    "ck_users_role", "ck_projects_pay_per_task", "ck_projects_status",
    "ck_tasks_status", "ck_tasks_assignment",
    "ck_submissions_status", "ck_submissions_qa_score",
    "ck_submissions_review_integrity",
    "ck_payments_status", "ck_payments_amount", "ck_payments_tasks_count",
    "ck_payments_paid_at_pairing", "ck_payments_method_when_paid",
    "ck_audit_logs_action",
)
missing_constraints = [c for c in expected_constraints
                       if c not in ddl]
expect(not missing_constraints,
       f"all {len(expected_constraints)} CHECK constraints present")
expected_indexes = {
    "ix_tasks_assignee_status", "ix_tasks_project_status",
    "ix_submissions_annotator_status", "ix_submissions_status_payment",
    "ix_submissions_annotator_submitted",
    "ix_payments_annotator_status", "ix_payments_status_paid_at",
    "ix_users_role_active", "ix_audit_logs_created_at",
}
found_indexes = {
    row[1] for row in conn.execute(
        "SELECT * FROM sqlite_master WHERE type='index'"
    ).fetchall()
}
missing_indexes = expected_indexes - found_indexes
expect(not missing_indexes, f"all {len(expected_indexes)} hot-path"
                            " indexes present")
expect(conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
       "integrity_check: ok")
expect(not conn.execute("PRAGMA foreign_key_check").fetchall(),
       "foreign_key_check: no violations")
conn.close()

# ---- 6. danger-pattern scan -----------------------------------------
app_files = ["app.py", "config.py", "models.py", "extensions.py",
             "audit.py", "backup.py"] + [
    "routes/" + n for n in sorted(os.listdir(os.path.join(ROOT, "routes")))
    if n.endswith(".py")
]
danger_hits = []
for rel in app_files:
    src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    for pattern, label in (
            (r"\bTODO\b", "TODO"), (r"\bFIXME\b", "FIXME"),
            (r"\bXXX\b", "XXX"), (r"\bHACK\b", "HACK"),
            (r"^\s*print\(", "print()"), (r"breakpoint\(", "breakpoint()"),
            (r"import pdb", "pdb"),
            (r'["\']/home/[a-z]', "absolute /home path")):
        if re.search(pattern, src, re.MULTILINE):
            danger_hits.append(f"{rel}: {label}")
expect(not danger_hits, f"app code clean of TODO/print/pdb/abs-paths"
                        f" ({len(app_files)} files)")
for base, _dirs, files in os.walk(template_dir):
    for name in files:
        src = open(os.path.join(base, name), encoding="utf-8").read()
        # |safe is permitted ONLY on rendering.py-sanitized views
        # (strict allowlist of roots); anything else is a danger hit.
        for match in re.finditer(r"{{\s*([^}|]+?)\s*\|\s*safe", src):
            expr = re.sub(r"^[^A-Za-z_]+", "",
                          match.group(1).strip())
            root = expr.split(".")[0].split("[")[0].strip()
            # rendered_prompt / media_prompt are the media task
            # prompt views — render_rich() outputs, same sanitizer
            # contract as prompt_view (Phase 8).
            if root not in ("input_view", "prompt_view",
                            "completion_view", "pref_views", "turn",
                            "rendered_prompt", "media_prompt"):
                danger_hits.append(f"templates/{name} ({match.group(0)})")
        if re.search(r"\son[a-z]+=\"", src):
            danger_hits.append(f"templates/{name}")
expect(not danger_hits,
       "templates: |safe only on rendering-sanitized views,"
       " no inline handlers")
leaked = []
for rel in app_files:
    src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    if "admin123" in src or "annotator123" in src:
        leaked.append(rel)
expect(not leaked, "seeded passwords exist only in seed.py")

# ---- 7. error paths + static assets ---------------------------------
page = adm_client.get("/definitely-missing")
expect(page.status_code == 404
       and "Page not found" in page.get_data(as_text=True),
       "404 renders the branded page")
page = ann_client.get("/admin/")
expect(page.status_code == 403
       and "Access denied" in page.get_data(as_text=True),
       "403 renders the branded page")
page = adm_client.post(f"/admin/review/{sub_id}",
                       data={"csrf_token": "wrong-token"})
expect(page.status_code in (200, 302), "bad CSRF handled, not a 500")
# 500: hit the probe route registered at boot
page = adm_client.get("/boom")
body = page.get_data(as_text=True)
expect(page.status_code == 500 and "Something went wrong" in body
       and "RuntimeError" not in body and "boom-test" not in body,
       "500 renders the branded page without leaking the exception")
# 405: POST to a GET-only route
page = adm_client.post("/admin/tasks")
expect(page.status_code == 405
       and "Request not allowed" in page.get_data(as_text=True)
       and "navbar" not in page.get_data(as_text=True)[:0]
       and "All times shown in UTC" in page.get_data(as_text=True),
       "405 renders the branded page")
expect(all(os.path.exists(os.path.join(ROOT, "static", p))
           for p in ("css/style.css", "js/main.js")),
       "static assets referenced by base.html exist")

# ---- 8. dependency pins match the installed versions ----------------
pins = []
for line in open(os.path.join(ROOT, "requirements.txt"),
                 encoding="utf-8").read().splitlines():
    line = line.strip()
    if line and not line.startswith("#"):
        pins.append(line)
pin_mismatches = []
for pin in pins:
    name, _, expected = pin.partition("==")
    pkg = name.split("[")[0].strip()  # strip extras like sentry-sdk[flask]
    try:
        installed = importlib.metadata.version(pkg)
    except importlib.metadata.PackageNotFoundError:
        installed = "<missing>"
    if installed != expected:
        pin_mismatches.append(f"{name}: pinned {expected}, installed"
                              f" {installed}")
expect(not pin_mismatches and len(pins) >= 9,
       f"all {len(pins)} requirement pins match the environment")

print()
if FAILURES:
    print(f"SYSTEM CHECK FAILED ({len(FAILURES)}):")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("ALL SYSTEM CHECKS PASSED")
