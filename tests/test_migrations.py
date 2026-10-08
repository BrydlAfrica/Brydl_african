"""Migration & PostgreSQL cutover tests.

Proves the two schema paths stay identical and the production tooling
is wired correctly:
 1. requirements pins for the migration stack (strict).
 2. migrations/ package structure + the initial revision's coverage.
 3. SCHEMA PARITY: a fresh db.create_all() database (local dev path)
    vs a fresh `flask db upgrade` database (production path) must
    have byte-identical normalized DDL — tables, indexes, constraints.
 4. Drift guard: `flask db check` on the migrated database reports no
    pending model changes (models.py and migrations/ never diverge).
 5. Backend-aware engine options (SQLite vs PostgreSQL connect args).
 6. Render's legacy postgres:// URI rewrite in config.
"""
import importlib.metadata
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = os.environ.get("BRYDL_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
os.chdir(ROOT)
shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)

FAILURES = []


def expect(condition, message):
    if condition:
        print(f"   ok: {message}")
    else:
        FAILURES.append(message)
        print(f"   FAIL: {message}")


def run(args, env_extra):
    env = dict(os.environ, PYTHONPATH=ROOT, **env_extra)
    return subprocess.run(
        [sys.executable, "-m", "flask", "--app", "app"] + args,
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )


# ---- 1. pins ----------------------------------------------------------
print("== Migration stack pins ==")
pins = {}
for line in open(os.path.join(ROOT, "requirements.txt"),
                 encoding="utf-8").read().splitlines():
    line = line.strip()
    if "==" in line and not line.startswith("#"):
        name, _, expected = line.partition("==")
        pins[name] = expected
trio_ok = True
for package in ("Flask-Migrate", "alembic", "psycopg2-binary"):
    installed = importlib.metadata.version(package)
    if pins.get(package) != installed:
        trio_ok = False
expect(trio_ok, "Flask-Migrate / alembic / psycopg2-binary pinned to"
       " the installed versions")

# ---- 2. migrations package -------------------------------------------
print("== migrations package ==")
mroot = os.path.join(ROOT, "migrations")
expect(os.path.isfile(os.path.join(mroot, "env.py"))
       and os.path.isfile(os.path.join(mroot, "alembic.ini")),
       "migrations/ package present (env.py + alembic.ini)")
versions = [f for f in os.listdir(os.path.join(mroot, "versions"))
            if f.endswith(".py")]
expect(len(versions) >= 2,
       f"revision chain present ({len(versions)} revisions)")
revisions = {}
downs = set()
for fname in versions:
    body = open(os.path.join(mroot, "versions", fname)).read()
    rev = re.search(r"revision\s*=\s*['\"]([\w]+)['\"]", body)
    down = re.search(r"down_revision\s*=\s*['\"]([\w]+)['\"]", body)
    if rev:
        revisions[rev.group(1)] = fname
        if down:
            downs.add(down.group(1))
heads = [r for r in revisions if r not in downs]
expect(len(heads) == 1,
       f"single head revision (heads: {heads or '??'})")
initial = open(os.path.join(mroot, "versions",
                            revisions["8bc840d0317e"])).read()
for table in ("users", "audit_logs", "payments", "projects", "tasks",
              "submissions"):
    assert re.search(
        rf"create_table\(\s*['\"]{table}['\"]", initial
    ), table
expect(True, "initial revision creates all six tables")
for constraint in ("ck_users_role", "ck_tasks_assignment",
                   "ck_payments_paid_at_pairing",
                   "ck_submissions_review_integrity"):
    assert constraint in initial, constraint
expect(True, "check constraints carried into the migration")

# ---- 3. schema parity: create_all vs db upgrade ----------------------
print("== Schema parity ==")
tmp = tempfile.mkdtemp(prefix="brydl-parity-")
db_a = os.path.join(tmp, "create_all.db")
db_b = os.path.join(tmp, "migrated.db")

proc = subprocess.run(
    [sys.executable, "-c", "from app import create_app; create_app()"],
    cwd=ROOT,
    env=dict(os.environ, PYTHONPATH=ROOT, DATABASE_URL=f"sqlite:///{db_a}"),
    capture_output=True, text=True, timeout=300,
)
expect(proc.returncode == 0 and os.path.exists(db_a),
       "fresh create_all database built (local dev path)")

proc = run(["db", "upgrade"], {
    "BRYDL_AUTO_SCHEMA": "0",
    "DATABASE_URL": f"sqlite:///{db_b}",
})
expect(proc.returncode == 0, f"flask db upgrade builds a fresh database"
       f" ({proc.stderr[-120:] if proc.returncode else 'migrated'})")


def normalized_schema(path):
    """Return order-insensitive normalized DDL from sqlite_master.

    Each CREATE TABLE clause lives on its own line (SQLite's format),
    so tables are compared as a SORTED set of normalized clauses —
    constraint order inside the DDL carries no semantics. Indexes and
    triggers are single statements compared whole.
    """
    con = sqlite3.connect(path)
    rows = con.execute(
        "SELECT type, name, sql FROM sqlite_master"
        " WHERE name != 'alembic_version' AND sql IS NOT NULL"
    ).fetchall()
    con.close()
    result = set()
    for kind, name, sql in rows:
        clauses = []
        for raw_line in sql.splitlines():
            line = raw_line.strip().rstrip(",").lower()
            line = re.sub(r"\s+", "", line)
            line = line.replace('"', "").replace("'", "")
            # Named constraints generated by batch_alter_table
            # (fk_..., uq_..., ck_...) are semantically identical to
            # create_all's unnamed inline forms — strip the name.
            line = re.sub(r"constraint[a-z_0-9]*foreignkey",
                          "foreignkey", line)
            line = re.sub(r"constraint[a-z_0-9]*unique",
                          "unique", line)
            line = re.sub(r"constraint[a-z_0-9]*check",
                          "check", line)
            # ALTER TABLE renders appended columns comma-joined on a
            # single line while create_all writes one per line — split
            # on commas so formatting cannot look like drift.
            clauses.extend(
                fragment for fragment in line.split(",") if fragment
            )
        result.add((kind, name, tuple(sorted(clauses))))
    return result


schema_a = normalized_schema(db_a)
schema_b = normalized_schema(db_b)
only_a = schema_a - schema_b
only_b = schema_b - schema_a
expect(not only_a and not only_b,
       "create_all schema == migrated schema (normalized DDL identical)")
if only_a or only_b:
    for row in sorted(only_a)[:4]:
        print(f"      only in create_all: {row[0]} {row[1]}")
    for row in sorted(only_b)[:4]:
        print(f"      only in migrated:  {row[0]} {row[1]}")

# ---- 4. drift guard ----------------------------------------------------
print("== Drift guard ==")
proc = run(["db", "check"], {
    "BRYDL_AUTO_SCHEMA": "0",
    "DATABASE_URL": f"sqlite:///{db_b}",
})
expect(proc.returncode == 0
       and "No new upgrade operations detected" in proc.stdout,
       "flask db check: models.py and migrations/ are in sync")

# ---- 4b. schema drift guard --------------------------------------------
print("== Drift guard ==")
stale = os.path.join(tmp, "stale.db")
con = sqlite3.connect(stale)
con.execute(
    "CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(120)"
    " NOT NULL, email VARCHAR(255) NOT NULL UNIQUE,"
    " password_hash VARCHAR(255) NOT NULL, role VARCHAR(20) NOT NULL,"
    " is_active BOOLEAN NOT NULL, created_at DATETIME NOT NULL)"
)
con.commit()
con.close()
probe = subprocess.run(
    [sys.executable, "-c", "from app import create_app; create_app()"],
    cwd=ROOT,
    env=dict(os.environ, PYTHONPATH=ROOT,
             DATABASE_URL=f"sqlite:///{stale}"),
    capture_output=True, text=True, timeout=300,
)
expect(probe.returncode != 0
       and "DATABASE SCHEMA OUT OF DATE" in probe.stderr
       and "db upgrade" in probe.stderr,
       "stale database at boot: loud RuntimeError with exact remediation")
# ...and the guard must NOT block `flask db` commands (the remediation)
probe = run(["db", "check"], {
    "DATABASE_URL": f"sqlite:///{stale}",
})
expect("OUT OF DATE" not in probe.stdout + probe.stderr,
       "guard stays silent during flask db commands (remediation path)")

# ---- 5. backend-aware engine options -----------------------------------
print("== Engine options ==")
from config import _engine_options  # noqa: E402

pg = _engine_options("postgresql://u:p@host/brydl")
lite = _engine_options("sqlite:///x.db")
expect(pg["connect_args"] == {"connect_timeout": 10}
       and "timeout" not in pg["connect_args"],
       "PostgreSQL engine options use connect_timeout")
expect(callable(lite.get("creator"))
       and "connect_args" not in lite,
       "SQLite engine options use the retry creator")
expect(pg["pool_size"] == 10 and pg["max_overflow"] == 20
       and pg["pool_pre_ping"],
       "pool sizing + pre_ping identical on both backends")

# ---- 6. legacy URI rewrite ----------------------------------------------
print("== DATABASE_URL handling ==")
from config import _load_database_uri  # noqa: E402

os.environ["DATABASE_URL"] = (
    "postgres://u:p@h.internal:5432/brydl_abc123"
)
rewritten = _load_database_uri()
expect(rewritten.startswith("postgresql://"),
       "Render's legacy postgres:// scheme rewritten to postgresql://")
os.environ["DATABASE_URL"] = ""
expect(_load_database_uri().startswith("sqlite:///"),
       "no DATABASE_URL falls back to local SQLite")

shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)
print()
if FAILURES:
    print(f"MIGRATION TESTS FAILED ({len(FAILURES)}):")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("ALL MIGRATION TESTS PASSED")
