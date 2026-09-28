#!/usr/bin/env bash
# Run every Brydl test suite against a throwaway database.
#
# WARNING: each suite wipes instance/ (your local dev database) and
# backups/. Run `flask --app app backup create` first if you need the
# current data.
#
# Usage:
#   bash tests/run_all.sh              # auto-detect python3
#   PYTHON=python3.11 bash tests/run_all.sh
set -u
cd "$(dirname "$0")/.."
export BRYDL_ROOT="$(pwd)"
export PYTHONPATH="$(pwd)"
# The job-runner thread must never poll while suites wipe the
# database under it; test_jobs drives the queue synchronously.
export BRYDL_DISABLE_JOBS=1
PY="${PYTHON:-python3}"

echo "Brydl Dashboard test battery (root: $BRYDL_ROOT)"
echo "Python: $("$PY" --version 2>&1)"
echo "----------------------------------------------------------------"

failed=0
for suite in tests/test_*.py; do
  rm -rf instance backups
  name="$(basename "$suite")"
  if "$PY" "$suite" >/tmp/brydl-test-out.log 2>/tmp/brydl-test-err.log; then
    summary="$(grep -E 'ALL .* PASSED' /tmp/brydl-test-out.log | tail -1)"
    printf "PASS  %-28s %s\n" "$name" "${summary:-}"
  else
    failed=1
    printf "FAIL  %-28s\n" "$name"
    echo "---- last output ----"
    tail -n 20 /tmp/brydl-test-out.log; echo "---- stderr ----"; tail -n 20 /tmp/brydl-test-err.log
    break
  fi
done

rm -rf instance backups
echo "----------------------------------------------------------------"
if [ "$failed" -ne 0 ]; then
  echo "RESULT: FAILED (stopped at first failing suite)"
  exit 1
fi
echo "RESULT: ALL SUITES PASSED"
