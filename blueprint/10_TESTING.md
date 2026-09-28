# 10 — Testing (the acceptance gates)

## The battery (must be green before ANY "done" claim)

**32 suites, 881 granular checks, flake8 clean** across the whole
repo (Phase 10 added `test_api.py` 34 checks + `test_jobs.py` 20 checks,
plus 2 carried-consensus checks: the parse matrix, auth matrix
(missing/unknown/revoked keys, no session fallback), scoping
(foreign projects uniform 404s), client-safe results contract, task
submission (validation, caps, paused refusal, audit), per-key rate
limit, and the admin issue/revoke flow with the show-once raw-key
contract; and the job suite (enqueue, synchronous run,
conditional-update claiming, exponential backoff, unknown-kind
parking, Paystack reconcile settled/failed/waiting/idempotent,
queued notification emails, kill switches and boot healing).
Previously (Phase 9 added `test_qualification.py`, 52 checks: the parse
matrix (good quiz, single option, missing/duplicate correct, orphan
option, empty, overlong, >30 questions), grading + integer rounding,
deterministic per-annotator order, payload degradation, console auth,
draft save + audit, bad-paste bounce that clobbers nothing, activation
with notification, active-quiz edit refusal, draft non-gating claim,
gate on claim (skips to free work) + assignment (reported skip) +
task-page redirect, answer-key secrecy, honest grading + canonical
sheet storage, attempt audit, pass terminal + no extra rows, claim +
task open after passing, duplicate-exemption refusal, exempt row
shape, revocation keeping failed history, retirement lifting the
gate, unknown quiz 404, CSRF redirect). Previously (Phase 8 added `test_media.py`, 39 checks: the sniffing matrix
(PNG/WAV/WebP/MP3 magic bytes, RIFF disambiguation, SVG and text
rejected, empty rejected), library auth (annotator 403), upload
validation (bad type, oversize via MEDIA_MAX_BYTES, dedupe flash +
no duplicate row, media.upload audit, per-project dedupe scope),
task-bound serving (holder gets bytes + stored content type,
non-holder 404, task-without-reference 404, unreferenced-asset
probe 404, staff route admin 200 / annotator 403), workspaces
(image renders + label choices + bogus-choice bounce + label+notes
document, dangling reference degrades to free-text label, audio
player + prompt + blank-transcript bounce + exact transcript
document), review + arbitration media display, and consensus
flagging divergent image labels for arbitration).
Previously (Phase 7 added `test_client_portal.py`, 43 checks: token
generation/hashing, log redaction, expiry, progress math,
client-safe records, admin issue/revoke, dead-link 404s, noindex,
approved-only deliveries without identity/gold, credential only in
link hrefs, audit coverage, raw-token absence from logs/audit).
Previously (Tier 2 added `test_paystack.py`, 36 checks: webhook signature
unit contract, bank matching, disabled fallback, recipient creation
+ caching, kobo money boundary, processing/success outcomes, audit
without account numbers, webhook settlement + idempotent retries +
failed path, CSRF/bank-mismatch guard rails, NDPR page. The
system-check CSRF matrix now carries an explicit, documented webhook
exemption). Previously (Phase 6 added `test_roles.py`, 48 checks: role vocabulary +
DB constraint, per-role surface access, cross-role 403 walls,
arbitration ruling by an auditor, sign-in routing, team creation of
qa accounts, and the rewritten landing copy; `test_brand.py`'s
landing needles updated to the new positioning). Previously (RLHF Phase 5 added `test_rubric.py`, 37 checks: rubric module
unit contract, project-form config, slider rendering + canonical-side
mapping under the deterministic flip, tampered/missing scores
rejected, review + arbitration display, export passthrough).
Previously (RLHF Phase 4 added `test_sft.py`, 53 checks: harvest-pipeline
unit contract, access control + gold secrecy, prefill/create with
the mandatory rewrite note, lifecycle + audit trail, export-freeze
consistency, CSRF-on-export. Note: Phase 3's "615 checks" was a
double-count — stray debug-run logs matched the tally glob; the true
23-suite figure was ~517. Counts are now taken from a clean scratch
directory). Previously (RLHF Phase 2 added `test_consensus.py`, 32 checks: depth form +
persistence, grouped import, claim/assign blindness guards, no-sibling-
leak, agreement detection, conflict + binding arbitration (frozen,
audited), rejection-withdrawal semantics, export consensus fields.
RLHF Phase 1 added `test_rlhf_preference.py`, 23 checks: workspace render + flip order,
gold isolation, justification minimum, canonical choice mapping,
review rendering, quality gold for preference, import paths +
confirm-time gold normalization, JSONL export). Runner:

```bash
rm -rf instance backups __pycache__        # ONLY from the shell,
for suite in tests/test_*.py; do           # NEVER inside a suite
  PYTHONPATH="$(pwd)" python "$suite"
done
flake8 .
```

Suites: annotator, annotator_experience, audit, brand, deploy, export,
hardening, import, migrations, offline, password_reset, payments,
review, rlhf_preference, scalability, stress60, system_check,
consensus, rendering, SFT builder, rubric, roles, paystack, client
portal,
system_fixes, tasks, tier1, user_mgmt.

## What each gate proves (highlights)

- **migrations (18 checks):** fresh `create_all` == fresh `flask db
  upgrade` (normalized DDL identical); `flask db check` clean; boot
  guard loud on stale DB, silent for `flask db`; PG connect_timeout;
  SQLite retry creator present.
- **stress60 (15 checks):** 60 concurrent annotators × 2 waves —
  exactly-once task assignment, p95 ≤ 409 ms, ~616 req/s reads.
  Permanent regression gate for "50+ without degradation".
- **tier1 (27–28 checks):** claim happy path (oldest-first,
  claimed_at, audit, PRG), CSRF refusal, daily cap, paused refusal,
  **9-way race = 9 distinct winners**, notification lifecycle
  (assigned→rejected→paid; badge 2→0 after feed view), deadlines,
  quality math (approval rate / avg QA / gold accuracy), annotator
  blocked from admin quality (403), terms gate.
- **system_fixes (30 checks):** bank ciphertext in DB (never the
  NUBAN), last4 + getter round-trip, masked Team page, CSV decrypts,
  legacy plaintext read, removal clears; revision cap (rounds 1–2
  stay, 3rd escalates → pool + notification + audit); COCO schema
  validity (keys, refs, categories, no gold), YOLO ZIP (classes.txt
  sorted, 5-column lines, manifest), CSV/JSONL unchanged, bogus
  format falls back, exports audited.
- **hardening:** lockout math, headers, CSRF enforcement, enum
  checks. **password_reset:** token hashing/TTL/single-use, rate
  limit, enumeration-safe responses, auth_version bump. **export:**
  all format/scope combos, gold exclusion, pseudonymity, formula
  guard. **scalability:** pagination caps, zero-N+1 patterns,
  indexes exist. **offline:** vendor assets present, SW + manifest
  served, no CDN references. **brand:** names/labels/theme toggle.
  **user_mgmt:** create/toggle/reset flows + audit + masked bank.
  **deploy:** Procfile/render.yaml/runtime/requirements sanity.

## The runner rule (a wound that must not re-open)

**Wipe `instance/` and `backups/` ONLY from the runner shell, BEFORE
the suite process starts.** A suite that `rmtree`s the instance dir
after `create_app()` strands every NEW pooled SQLite connection with
`unable to open database file` → fail-closed user_loader → racers
become anonymous → 302 to `/login` with NO error shown. This cost a
full debugging cycle (see 11_BUILD_HISTORY.md). Suites document this
in a comment; the shell does the wiping.

## Test-writing conventions

- Each suite: importable module, `expect(cond, label)` granular
  checks, prints `ALL … TESTS PASSED`, exit 0/1; battery greps for
  `FAIL:`.
- Capture ORM ids inside the app_context before exiting it; query DB
  only inside a context.
- Flashes are HTML-escaped — grep the escaped form.
- When diagnosing auth redirects, capture the POST's `Location`
  header, not the followed page body.
- Engine-options tests: assert `callable(options["creator"])` (NOT
  `connect_args`) for SQLite.
- Fresh pending tasks must be seeded for admin-assign phases when an
  earlier phase (race) consumed the pool tail.

## Pre-ship checklist per change

1. New/changed behavior covered by granular checks in the right suite.
2. Full battery green (29/29) — not just the touched suite.
3. `flake8 .` clean (PEP 8 + docstrings).
4. Blueprint docs updated in the same change (see blueprint/README).
