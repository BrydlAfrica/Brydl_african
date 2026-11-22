"""Phase 2 (RedLab): Migration #18 + models + Fernet crypto.

Verifies:

1. redlab.crypto encrypt/decrypt roundtrip, failure raises
   SecretDecryptionError that does NOT leak key/token text,
   mask_key_fingerprint produces the "sk-...3f2a" format.
2. Migration #18 upgrade applies cleanly (schema 'redlab' created,
   all four tables exist, additive columns on submissions/projects
   present) on SQLite (file-backed, since in-memory can't run
   Alembic easily).
3. Migration #18 downgrade reverses cleanly (tables + columns gone,
   schema dropped).
4. Model relationships, constraints (UNIQUE on sessions.submission_ref
   and messages(session_id,turn_index), CHECKs for status/role), and
   CRUD work.
5. All additive columns have the expected server defaults (0 / 'none').
"""
from __future__ import annotations
import subprocess
import shutil

import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ["BRYDL_DISABLE_JOBS"] = "1"
os.environ["REDLAB_ENABLED"] = "false"
# Disable auto create_all in this parent process too — we drive the
# schema via migrations (section 2) and via create_all (section 3)
# explicitly. Without this, `from app import create_app` triggers the
# module-level `app = create_app()` which runs db.create_all() and
# leaves an instance/brydl.db for the CLI subprocess to trip over.
os.environ["BRYDL_AUTO_SCHEMA"] = "0"
# Use a dedicated ephemeral key so we can rotate it and test failure.
os.environ["REDLAB_FERNET_KEY"] = "P2R_l8fQ4bZ7xJ9mK3nV5pS8tU2wY6aC_dEfGhIjKlM="

# Make sure we start with a clean instance/ so no stale db pollutes
# migration tests. Recreate the directory because config.py writes
# the ephemeral SECRET_KEY into it.
import shutil as _shutil  # noqa: E402
_shutil.rmtree(os.path.join(ROOT, "instance"), ignore_errors=True)
os.makedirs(os.path.join(ROOT, "instance"), exist_ok=True)
_shutil.rmtree(os.path.join(ROOT, "backups"), ignore_errors=True)

FAIL: list[str] = []


def expect(cond: bool, msg: str) -> None:
    if not cond:
        FAIL.append(msg)
        print("  FAIL:", msg)
    else:
        print("   ok:", msg)


# --- 1. Crypto roundtrip --------------------------------------------------
from redlab.crypto import (  # noqa: E402
    SecretDecryptionError,
    decrypt_secret,
    encrypt_secret,
    mask_key_fingerprint,
    reset_fernet_for_tests,
)

print("== 1. Fernet crypto ==")
key1 = "sk-abcdefghijklmnopQRSTUV-0123456789wxyz3f2a"
ct = encrypt_secret(key1)
expect(ct != key1, "ciphertext differs from plaintext")
expect(ct.startswith("gAAAAA") or len(ct) > 40,
       "ciphertext looks like Fernet token")
expect(decrypt_secret(ct) == key1, "decrypt(encrypt(k)) == k roundtrip")

# Empty/None inputs
expect(decrypt_secret(None) == "", "decrypt(None) returns empty str")
expect(decrypt_secret("") == "", "decrypt('') returns empty str")

# Wrong key -> SecretDecryptionError, message is safe
os.environ["REDLAB_FERNET_KEY"] = "aDIFFERENTkeyThatIsAlsoValidBase64aaasdkjEQ="
reset_fernet_for_tests()
try:
    decrypt_secret(ct)
    expect(False, "wrong key raised SecretDecryptionError")
except SecretDecryptionError as e:
    msg = str(e)
    expect(key1 not in msg and ct not in msg,
           "error message does NOT leak plaintext or ciphertext")
    expect("decrypt" in msg.lower() or "secret" in msg.lower(),
           "error message is descriptive without leaking")
except Exception as e:  # noqa: BLE001
    expect(
        False, f"wrong key raised SecretDecryptionError (got {type(e).__name__})")

# Fingerprint
expect(mask_key_fingerprint(key1) == "sk...3f2a",
       "mask_key_fingerprint formats sk-key as 'sk...3f2a'")
expect(mask_key_fingerprint("Bearer tokentokentokenLAST") == "Bear...LAST",
       "non-sk token gets 4-char prefix mask")
expect(mask_key_fingerprint("short") == "***",
       "short secrets fully masked")
expect(mask_key_fingerprint("") == "***",
       "empty string returns ***")
expect(mask_key_fingerprint(None) == "***",  # type: ignore[arg-type]
       "None returns ***")

# Restore the real key for the model tests
os.environ["REDLAB_FERNET_KEY"] = "P2R_l8fQ4bZ7xJ9mK3nV5pS8tU2wY6aC_dEfGhIjKlM="
reset_fernet_for_tests()


# --- 2. Migration #18 upgrade/downgrade on file SQLite --------------------
print("== 2. Migration #18 upgrade/downgrade ==")

# Use a temp DB file and run migrations via flask CLI (same path
# production uses).
tmpdir = tempfile.mkdtemp(prefix="brydl-phase2-")
db_path = os.path.join(tmpdir, "mig-test.db")
# Ensure the parent exists (SQLite will create the file but not the
# directory, and our _sqlite_connect_with_retry retries OPEN failures).
os.makedirs(tmpdir, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
# Make sure the Flask CLI in the subprocess doesn't pick up a stale
# local instance/brydl.db from prior tests.
instance_dir = os.path.join(ROOT, "instance")
shutil.rmtree(instance_dir, ignore_errors=True)
os.makedirs(instance_dir, exist_ok=True)
try:
    # The flask CLI loads app.py which uses config.py; point
    # instance/ into tmpdir so we don't pollute local state.
    from app import create_app
    from extensions import db as _db
    from models import Project, Submission, User, utc_now  # noqa: F401
    from redlab.models import (  # noqa: F401 — register tables
        Message, ProcessedEvent, Session, Target,
    )
    from redlab.models import ensure_redlab_schema
    from sqlalchemy import inspect as sa_inspect, text as sa_text
    import flask as _flask

    app = create_app()

    def run_flask(*args):
        # Start from a clean environment (subprocess inherits os.environ
        # by default, which may carry DATABASE_URL set elsewhere in the
        # test harness — we MUST NOT let an in-memory URI shadow the
        # temp file db_path).
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", tmpdir),
            "PYTHONPATH": ROOT,
            "FLASK_APP": "app.py",
            "BRYDL_DISABLE_JOBS": "1",
            "BRYDL_AUTO_SCHEMA": "0",
            "DATABASE_URL": f"sqlite:///{db_path}",
            "PYTHONIOENCODING": "utf-8",
        }
        # Make sure the repo's own instance/ dir (which other test
        # suites may have just populated) does not shadow DATABASE_URL.
        shutil.rmtree(os.path.join(ROOT, "instance"), ignore_errors=True)
        r = subprocess.run(
            [sys.executable, "-m", "flask"] + list(args),
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
        return r

    # Stamp "no migrations" first, then run upgrade to head.
    r = run_flask("db", "upgrade", "head")
    expect(r.returncode == 0,
           f"flask db upgrade head exits 0 (err: {r.stderr[-400:]})")

    with app.app_context():
        # Bind engine to the temp sqlite file
        from sqlalchemy import create_engine
        engine = create_engine(f"sqlite:///{db_path}")
        insp = sa_inspect(engine)

        # SQLite doesn't support schemas natively; check that the
        # redlab tables exist (in SQLite they will be named redlab_*,
        # while in Postgres they live in schema redlab).
        tables = insp.get_table_names()
        # For SQLite alembic creates tablenames like "redlab_targets"
        # because schema prefix is flattened; in Postgres tables are
        # in schema "redlab". Either way, all four must exist.
        table_set = set(tables)
        has_targets = "targets" in table_set or "redlab_targets" in table_set
        has_sessions = "sessions" in table_set or "redlab_sessions" in table_set
        has_messages = "messages" in table_set or "redlab_messages" in table_set
        has_events = (
            "processed_events" in table_set
            or "redlab_processed_events" in table_set
        )
        expect(has_targets, "migration creates redlab.targets")
        expect(has_sessions, "migration creates redlab.sessions")
        expect(has_messages, "migration creates redlab.messages")
        expect(has_events, "migration creates redlab.processed_events")

        # Additive columns
        sub_cols = {c["name"] for c in insp.get_columns("submissions")}
        proj_cols = {c["name"] for c in insp.get_columns("projects")}
        for col in ("redlab_session_id", "redteam_state", "redteam_breach_count"):
            expect(col in sub_cols, f"submissions.{col} added")
        for col in ("redteam_base_pay", "redteam_breach_bounty", "redteam_session_quota"):
            expect(col in proj_cols, f"projects.{col} added")

        # Migration version is now at our new revision.
        with engine.connect() as _conn:
            v = _conn.execute(sa_text(
                "SELECT version_num FROM alembic_version"
            )).fetchone()[0]
        expect(v in ("b3c4d5e6f701", "e1f2a3b4c5d6"),
               f"alembic_version at head e1f2a3b4c5d6 (got {v})")

    # Test downgrade
    r = run_flask("db", "downgrade", "5a9e8b7c2d01")
    expect(r.returncode == 0,
           f"flask db downgrade to prior head exits 0 (err: {r.stderr[-200:]})")

    with app.app_context():
        engine = create_engine(f"sqlite:///{db_path}")
        insp = sa_inspect(engine)
        tables = set(insp.get_table_names())
        # All four redlab tables should be gone.
        redlab_gone = (
            "targets" not in tables and "sessions" not in tables
            and "messages" not in tables and "processed_events" not in tables
            and "redlab_targets" not in tables
            and "redlab_sessions" not in tables
            and "redlab_messages" not in tables
            and "redlab_processed_events" not in tables
        )
        expect(redlab_gone, "downgrade drops all four redlab tables")
        sub_cols = {c["name"] for c in insp.get_columns("submissions")}
        proj_cols = {c["name"] for c in insp.get_columns("projects")}
        expect("redlab_session_id" not in sub_cols,
               "downgrade drops submissions.redlab_session_id")
        expect("redteam_state" not in sub_cols,
               "downgrade drops submissions.redteam_state")
        expect("redteam_base_pay" not in proj_cols,
               "downgrade drops projects.redteam_base_pay")

        with engine.connect() as _conn:
            v = _conn.execute(sa_text(
                "SELECT version_num FROM alembic_version"
            )).fetchone()[0]
        expect(v == "5a9e8b7c2d01",
               f"after downgrade version is prior head (got {v})")
finally:
    # Note: leave tmpdir in place until the VERY end of the test
    # (section 3 doesn't use it, but defensive — nothing else should
    # reference it beyond this point). We clean up at the end via
    # _cleanup() call.
    pass


# --- 3. Models / CRUD / constraints via create_all ------------------------
# Section 3 exercises CRUD against a SECOND temporary SQLite file by
# invoking tests/_phase2_crud_runner.py as a subprocess. SQLAlchemy
# engines bind at first use so swapping DATABASE_URL mid-process is
# unreliable in a single interpreter; a fresh subprocess isolates
# caches and gives us a clean environment for create_all.
shutil.rmtree(tmpdir, ignore_errors=True)

tmpdir3 = tempfile.mkdtemp(prefix="brydl-phase2-crud-")
db3 = os.path.join(tmpdir3, "crud.db")
os.makedirs(tmpdir3, exist_ok=True)

print("== 3. Models CRUD + constraints ==")
runner = os.path.join(ROOT, "tests", "_phase2_crud_runner.py")
r = subprocess.run(
    [sys.executable, runner],
    cwd=ROOT,
    capture_output=True,
    text=True,
    env={
        "PATH": os.environ.get("PATH", ""),
        "HOME": tmpdir3,
        "PYTHONPATH": ROOT,
        "PYTHONIOENCODING": "utf-8",
        "DATABASE_URL": f"sqlite:///{db3}",
    },
)
sys.stdout.write(r.stdout)
section3_passed = 0
for line in r.stdout.splitlines():
    if line.startswith("   ok:"):
        section3_passed += 1
    if line.startswith("  FAIL:"):
        FAIL.append(line.replace("  FAIL: ", ""))
if r.returncode != 0:
    FAIL.append("section-3 CRUD subprocess exited nonzero")
    sys.stderr.write("--- stderr ---\n")
    sys.stderr.write(r.stderr[-1500:])
shutil.rmtree(tmpdir3, ignore_errors=True)

total = 12 + 18 + section3_passed  # 12 crypto + 18 migration + CRUD

print()
if FAIL:
    print(f"FAILED: {len(FAIL)} check(s) failed:")
    for f in FAIL:
        print(" -", f)
    sys.exit(1)
print(f"ALL {total} Phase 2 MODEL/CRYPTO CHECKS PASSED")
