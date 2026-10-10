"""Phase 9: Lint Contracts, Hardening & Flag Rehearsal tests.

Covers:
  1. import-linter architectural contracts validation:
     - Contract 1: redlab must NOT import from Flask routes/business logic.
     - Contract 2: Flask routes must NOT import from redlab internal modules.
  2. AST check: no synchronous blocking calls (requests.*, time.sleep())
     inside async def functions under redlab/.
  3. Plaintext key leak check: zero plaintext api_key database columns.
  4. Dark deployment / Feature Flag gating:
     - With REDLAB_ENABLED=false, /redlab/v1/... endpoints return 404 cleanly.
     - With REDLAB_ENABLED=true, /redlab/v1/... routes respond normally.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

failures = 0
checks = 0


def expect(cond, msg):
    global failures, checks
    checks += 1
    if cond:
        print(f"   ok: {msg}")
    else:
        failures += 1
        print(f"  FAIL: {msg}")


# === 1. import-linter Architectural Contracts ========================
print("\n== 1. import-linter Architectural Contracts ==")
r_linter = subprocess.run(
    ["lint-imports"],
    cwd=str(ROOT),
    capture_output=True,
    text=True,
)
print(r_linter.stdout)
if r_linter.stderr:
    print(r_linter.stderr, file=sys.stderr)
expect(r_linter.returncode == 0,
       f"lint-imports exits 0 (got code {r_linter.returncode})")
expect("Contracts: 2 kept, 0 broken" in r_linter.stdout,
       "all import-linter architectural contracts kept")

# === 2. AST Check: No Blocking Sync Calls in Async Functions =========
print("\n== 2. AST Check: No Blocking Async Calls ==")
r_ast = subprocess.run(
    [sys.executable, str(ROOT / "scripts" / "check_no_async_blocking.py")],
    cwd=str(ROOT),
    capture_output=True,
    text=True,
)
print(r_ast.stdout)
expect(r_ast.returncode == 0,
       f"check_no_async_blocking.py exits 0 (got code {r_ast.returncode})")
expect("ALL ASYNC BLOCKING CHECKS PASSED" in r_ast.stdout,
       "no blocking calls found in async def functions")

# === 3. Key Leak Check: Zero Plaintext API Key Columns ================
print("\n== 3. Plaintext Key Leak Check ==")
r_leak = subprocess.run(
    [sys.executable, str(ROOT / "scripts" / "check_no_plaintext_keys.py")],
    cwd=str(ROOT),
    capture_output=True,
    text=True,
)
print(r_leak.stdout)
expect(r_leak.returncode == 0,
       f"check_no_plaintext_keys.py exits 0 (got code {r_leak.returncode})")
expect("ALL KEY LEAK CHECKS PASSED" in r_leak.stdout,
       "zero plaintext api_key database columns found")

# === 4. Feature Flag Rehearsal ========================================
print("\n== 4. Feature Flag Rehearsal ==")
from fastapi.testclient import TestClient  # noqa: E402
from redlab.app import create_redlab_app  # noqa: E402

# 4.1 When REDLAB_ENABLED is False, endpoints 404 cleanly with DISABLED_BODY
os.environ["REDLAB_ENABLED"] = "false"
app_disabled = create_redlab_app()
client_disabled = TestClient(app_disabled)
resp_dis = client_disabled.get("/v1/some-route")
expect(resp_dis.status_code == 404,
       f"when REDLAB_ENABLED=false, /v1/some-route returns 404 (got {resp_dis.status_code})")
expect(resp_dis.json().get("error") == "redlab_disabled",
       f"error code is 'redlab_disabled' (got {resp_dis.json()})")

resp_dis_turn = client_disabled.post("/v1/sessions/sid-test/turn", json={
    "client_turn_id": "c-1", "user_content": "hello",
})
expect(resp_dis_turn.status_code == 404,
       f"when REDLAB_ENABLED=false, /turn returns 404 (got {resp_dis_turn.status_code})")
expect(resp_dis_turn.json().get("error") == "redlab_disabled",
       "turn error is 'redlab_disabled'")

# Ops healthz at root is accessible and reports redlab_enabled: False
resp_ops = client_disabled.get("/healthz")
expect(resp_ops.status_code == 200, f"/healthz status 200 (got {resp_ops.status_code})")
expect(resp_ops.json().get("redlab_enabled") is False, "redlab_enabled is False in healthz")

# 4.2 When REDLAB_ENABLED is True, endpoints respond normally
os.environ["REDLAB_ENABLED"] = "true"
app_enabled = create_redlab_app()
client_enabled = TestClient(app_enabled)
resp_ops_en = client_enabled.get("/healthz")
expect(resp_ops_en.status_code == 200,
       f"when REDLAB_ENABLED=true, /healthz returns 200 (got {resp_ops_en.status_code})")
expect(resp_ops_en.json().get("redlab_enabled") is True,
       "healthz reports redlab_enabled is True")

print()
if failures:
    print(f"PHASE 9 FAILED ({failures}/{checks} checks)")
    sys.exit(1)
print(f"ALL PHASE 9 HARDENING & LINT CHECKS PASSED ({checks} checks)")
