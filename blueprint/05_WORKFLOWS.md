# 05 — Workflows (the lifecycle contracts)

## Task lifecycle

```
pending ── assign(admin) ──────────────► assigned ── submit ──► submitted
pending ── claim(annotator, self) ────►                    │
            (claimed_at stamped; cap 20/day)                │
assigned ── unassign(admin) ──────────► pending             │
submitted ── approve ─────────────────► approved            │
submitted ── reject(+return_for_revision) ─► rejected      │
rejected ── resubmit ─────────────────► submitted           │
submitted ── reject WITHOUT keep ─────► pending (unowned)   │
submitted ── reject, count ≥ 3 ───────► pending (unowned) + task.escalated
```

Invariants (DB-enforced): `assigned_to IS NULL ⇔ status='pending'`;
a task with an approved submission never returns to pending; every
transition goes through a **conditional UPDATE** (status matches the
pre-transition value) so concurrent reviews/claims can never
double-fire.

## Self-claim (race-safe) — exact contract

1. CSRF → annotator active → terms accepted.
2. Daily cap: count tasks claimed today (`claimed_at` same UTC day) ≥
   `CLAIM_DAILY_CAP` (20) ⇒ friendly refusal flash.
3. Up to 3 attempts: pick oldest pending task (oldest active project
   first), then a single **conditional UPDATE**
   `WHERE id = :picked AND status = 'pending' SET status='assigned',
   assigned_to=:me, claimed_at=now()`. Rowcount 0 = someone took it →
   sleep 0.05·(attempt+1) → retry.
4. Audit `task.claim` (task id only). Flash "claimed"/"pool empty".
5. **9-way parallel claims yield 9 distinct winners** — this exact
   property is a regression test.

## Review pipeline (admin)

- Verdicts apply to submissions still `submitted` (conditional).
- Approve: submission `approved`, task `approved`, score/notes stored.
- Reject + "return for revision": submission `rejected`, task
  `rejected` (stays with annotator) → notification `task.rejected`
  (in-app + email if SMTP).
- Reject without keep: task returns to pool unowned.
- **Revision cap:** for each rejected task, count ALL its rejected
  submissions (any annotator) ≥ `TASK_MAX_REJECTIONS` (3) ⇒ task force-
  returned to pool + `task.escalated` notification + escalation count
  in audit detail + admin flash mentions it. The loop always terminates.
- Rejection notifications include the QA notes pointer ("open the task
  to see the reviewer's notes").

## Preference (RLHF/DPO) contract

- Task input_data: JSON `{kind:"preference", prompt, response_a,
  response_b}`. Gold (optional): canonical "a"/"b".
- Display order is a **deterministic hash of (task_id, annotator_id)**
  (~50/50 position randomization), computed identically on GET and
  POST — the client never sends order state.
- Submission stores the CANONICAL choice, the seen order, and a
  required justification (>= PREFERENCE_MIN_JUSTIFICATION, default
  40) — the justification is the arbitration evidence.
- Review shows canonical columns + choice + justification; quality
  gold accuracy compares `chosen` vs the gold key.
- Import: both paths accept JSON preference lines; gold normalizes to
  a/b at preview AND at confirm.
- **Rubric dimensions (RLHF Phase 5):** a preference project may
  define named dimensions (project form, one per line, capped by
  MAX_RUBRIC_DIMENSIONS/MAX_RUBRIC_DIM_NAME, parsed by rubric.py).
  Annotators then rate BOTH responses on every dimension with NATIVE
  range sliders (no client-side logic); fields are named by INDEX
  (rubric_0_a…) so free-text names never touch field ids, map to the
  CANONICAL side exactly like the choice radios, and are
  server-validated ints on the RUBRIC_SCALE range. Scores ride the
  output document as {"rubric": {dim: {"a": x, "b": y}}}; review
  detail renders them as a table, arbitration as badges — only when
  they validate strictly against the project's dimensions. Consensus
  verdict keys stay the chosen letter (rubric never changes agreement).
- Exports: JSONL/CSV carry full documents (rubric included when
  present); COCO falls back to a single "preference" category.

## Consensus (RLHF Phase 2) contract

- A project with `required_annotations` 2–3 turns every imported row
  into ONE ConsensusGroup with exactly that many identical sibling
  tasks (both import paths).
- **Blindness is structural**: siblings are ordinary tasks; the claim
  route and admin assign both refuse to give one annotator a second
  sibling of a group they already hold; annotator surfaces never
  render sibling data (tested).
- Evaluation runs on every submission and every QA verdict: with all
  siblings answered, statuses AGREE (same verdict key: preference
  chosen / classification label) → `agreed`; disagree, or any
  free-text answer → `conflict`; a sibling whose latest submission is
  rejected (being redone) withholds its verdict → `pending`.
- Arbitration (`/admin/review/conflicts`): auditor sees both verdicts
  + justifications, records a BINDING ruling (choice + note); the
  group freezes as `resolved` — later revisions never unfreeze it.
  Audited with task ids only.
- Exports carry `consensus` (status + decision) on JSONL records and
  `consensus_status` / `consensus_decision` CSV columns.

## Trusted-rendering contract (RLHF Phase 3)

- Task content is UNTRUSTED INPUT: rendered SERVER-SIDE only
  (rendering.py: markdown → HTML → nh3 strict allowlist sanitizer).
  No client-side rendering, no third-party JS.
- `render_rich(text)` → sanitized HTML (headings, lists, tables,
  links http/https/mailto only, fenced code highlighted by Pygments
  with CSS classes from self-hosted static/css/pygments.css).
- `content_view(text)` → conversation documents
  ({"kind":"conversation","turns":[{role, text},…], roles
  user/assistant/system}) render as chat bubbles; everything else as
  rich HTML. Invalid/oversize (> MARKDOWN_RENDER_MAX_CHARS) content
  degrades to escaped plain text. The module NEVER raises.
- Applied to: annotator workspace input + preference prompt/responses,
  review-detail input, arbitration prompt. Stored data and exports
  stay RAW (display-time only, no migration).
- Templates mark these views `|safe` — the system-check suite allows
  |safe ONLY on the sanitizer-backed roots (input_view, prompt_view,
  pref_views, turn).

## SFT gold-rewrite contract (RLHF Phase 4)

- sft.py (pure, never raises) harvests each finalized task into a
  (prompt, source_completion, source) triple. Harvest priority for
  the completion: RESOLVED consensus ruling (preference resolves to
  the decided response's full text; other kinds to the decided
  verdict) > latest APPROVED submission output > seed gold
  expected_output. The prompt side: preference prompt, conversation
  last-user-turn, else text/prompt/raw.
- The admin builder (admin-only, /admin/sft) shows the harvested
  source immutable next to the editable pair; saving a completion
  that differs from source_completion REQUIRES a written edit note
  (deviation clause). Status lifecycle draft → ready → exported;
  exported is terminal and frozen (tampering POST refused), drafts
  alone are deletable.
- Export (POST only — it mutates): chat-messages JSONL
  ({"messages":[{role,content},…]}) capped at SFT_EXPORT_MAX_ROWS;
  the flip to exported commits in the same transaction as the audit
  event, so the shipped file and the database stay provably in sync.
- Every pair carries provenance (validated source task id) and the
  full lifecycle is audited: sft.create / sft.update / sft.delete /
  sft.export.

## Payout pipeline

1. `/payments/generate`: sweep every annotator's approved submissions
   with `payment_id IS NULL` → one Payment row (amount = Σ
   pay_per_task, Decimal; tasks_count; period). Idempotent.
2. `/payments/<id>/mark-paid`: set status paid + method + paid_at →
   `payout.paid` notification (amount + method).
3. `/payments/bank-batch`: CSV of pending payments for bank upload —
   the **only** place NUBANs are decrypted. Download→bank→delete is the
   operator procedure. Audited (count + totals, never contents).

## Notifications contract

- `notify(user, kind, payload, subject, body, commit=False)`:
  inserts the in-app row inside the caller's transaction (never
  raises); when SMTP is configured AND a subject is given, also queues
  an email with a `SITE_URL`-based link.
- Kinds: `task.assigned`, `task.rejected`, `task.escalated`,
  `payout.paid`.
- Bell badge = unread count (context processor; failure ⇒ 0).
- Feed shows 50 newest, marks all read on view (one conditional
  UPDATE for that user).

## Password reset flow

forgot-password → token (32B urlsafe; only sha256 stored, single-use,
TTL 3600 s, per-address rate limit 3/15 min) → emailed (or app-log
fallback in dev) → reset form → set new password + `auth_version += 1`
(kills all sessions) + clear token. Same generic response whether or
not the address exists.

## Terms gate

`TERMS_VERSION` (env `BRYDL_TERMS_VERSION`, default "1.0"). Annotator
with `terms_version != current` gets every route 302-redirected to
`/annotator/terms` (except terms/accept/continue). Accepting stores
version + UTC timestamp. **Bumping the version re-prompts everyone.**
Accounts created by admin start unaccepted; seeded demo accounts are
pre-accepted.

## Qualification gate (Phase 9)

A project may carry ONE qualification quiz (`qualification_quizzes`,
unique per project). While it is **active**, every path into the
project's work demands a `passed` (or `exempt`) `quiz_attempts` row:
self-claim skips gated projects (skipping to older free work), admin
assignment reports gated tasks as skipped with a warning, and the
task page redirects the annotator to the quiz. Draft/retired quizzes
gate nothing. Exemptions are admin-granted, audited, revocable; the
roster shows exactly who qualifies. Attempt rules: max_attempts
counted from failed tries; a pass is terminal; the correct-answer key
never leaves the server (options are rendered as text only, grading
is server-side against the canonical payload).

## Machine API (Phase 10)

Project-scoped Bearer keys (`brydl_` + 32 urlsafe bytes) live under
`/admin/projects/<id>/api-keys` — the raw key flashes exactly once
(show-once contract), only its sha256 is stored, revoke is instant.
The API carries no cookies and never participates in a browser
session: there is no CSRF surface by construction, rate limiting is
per-key (default 60/minute) and the response shape matches the client
portal contract (no annotator identity, no gold, no reviewer notes).
POST `/tasks` enforces caps and runs fail-closed validation; audited
as counts only.

## Background jobs (Phase 10)

A DB-backed queue (`background_jobs`) takes anything that shouldn't
block a request: today (1) SMTP delivery for notification emails
(jobs run → emails move off the hot path), (2) Paystack reconciliation
(pending payouts older than the age cutoff are verified against the
gateway once per poll cycle; settlements are idempotent with the
webhook pending-guard). The in-process runner thread starts at app
boot (kill switches: `JOBS_ENABLED=0` and `BRYDL_DISABLE_JOBS=1`);
claiming is a conditional UPDATE (queued→running) so N web workers
or a future dedicated `worker.py` dyno can poll the same table
without double execution, and boot healing requeues rows a crash
left in `running`. Redis is optional: `kvstore.bump_window` falls
back to per-process counters when `REDIS_URL` is empty (single
gunicorn process is enough for startup volume).

## CSV import pipeline

upload/paste → parse with limits (≤2000 rows, ≤2 MB) → **preview
screen**: column-map validation (project, text/kind, optional gold),
per-row errors with line numbers, counts → confirm → bulk insert.
Never partially commits on validation failure. Template download for
clients.

## Project export pipeline (all formats)

scope filter (approved / reviewed / all) → `yield_per=500` stream →
format renderer (see 07) → in-memory body → `Content-Disposition`
attachment → audit `project.export` (format, scope, row count — never
data). Annotator ids are pseudonymous everywhere.
