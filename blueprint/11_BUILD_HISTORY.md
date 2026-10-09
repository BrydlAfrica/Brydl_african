# 11 — Build history (first prompt → now)

A dated ledger of everything built, broken, decided, and fixed. A
faithful rebuild reproduces the features; this chapter explains WHY
they look the way they do.

## Phase 1 — Foundation (initial build)

- Application factory, 4 blueprints, 7-table models, money rules,
  UTC, PEP 8 + docstrings discipline established from commit one.
- Auth: login, roles, deactivation, lockout; admin console:
  projects CRUD, tasks grid, manual assign/unassign, review
  (approve/reject + score + notes), payments core, team management;
  annotator workspace: queue, task workspace, history.
- Audit trail from day one (log_event; counts only, never data).
- Hardening: security headers, CSP, CSRF everywhere, login lockout,
  import/export limits, generic auth errors.
- CSV import pipeline (paste or file → validation preview → confirm)
  with template download; project export JSONL/CSV with scope + gold
  toggle; formula-injection guard.
- Password reset (hashed single-use tokens, rate limits, enumeration-
  safe; SMTP-optional with dev log fallback); in-account password
  change.
- Offline-first PWA: manifest, service worker, offline page,
  light/dark theme per user.
- Terms & conditions gate (versioned; admin-created accounts gated,
  seeds pre-accepted).
- Annotator payout profile: bank name/number/name/type + phone with
  NUBAN validation (leading zeros preserved as text).
- Scalability work: indexes, 20/page pagination, zero-N+1,
  streaming exports; stress gate 60×2 (exactly-once, p95 ≤ 409 ms).
- Migration chain grew to #6; 18 legacy suites green; flake8 clean.

## Phase 2 — Production (Render)

- GitHub push (`brydldata-lab`), Render web service + Oregon
  Postgres, gunicorn, `BRYDL_AUTO_SCHEMA=0` boot-guard pattern, seed
  admin in prod (audited id #1).
- **Production login outage → fixed:** the CDN-hosted Bootstrap was
  unreachable from the operator's network; resolution: **self-host
  Bootstrap 5.3.8 + Icons 1.13.1 in `static/vendor/`** (offline-first
  rule now permanent; SRI hashes dropped in favor of vendoring).
- Region lesson: service provisioned in **Oregon**; a `region: fra`
  leftover in render.yaml must never be recreated (Frankfurt).
- SMTP env vars prepared by operator; end-to-end email test DEFERRED
  by operator ("we fix that later").

## Phase 3 — Tier 1 (operations at scale)

- **Self-claim**: oldest-pending claim route, `CLAIM_DAILY_CAP=20`,
  race-safe conditional UPDATE (proven 9-way = 9 distinct winners),
  `task.claim` audit, cap badge + Claim button on dashboard.
- **Notifications**: `notifications` table (migration #7), kinds
  assigned/rejected/paid, bell + unread badge (context processor),
  50-newest feed marks-read-on-view, email leg via SMTP when set,
  `SITE_URL` for clickable links.
- **Deadlines**: project form → list → annotator queue badge.
- **Quality dashboard**: per-annotator approval rate, avg QA score,
  gold accuracy (strip+lower match), last-30-day throughput.
- **The claim-race debugging saga (root cause, not the route):** the
  suite failed at 3/9 winners. Instrumentation misled (followed
  redirects landed on the login page; racers were ANONYMOUS because
  the suite's in-process `rmtree("instance")` deleted the DB
  directory after `create_app()`, so every NEW pooled SQLite
  connection raised CANTOPEN while warm pooled connections kept
  working — fail-closed loader did the rest). **Rule: only the runner
  shell wipes instance/backups, before process start.** Hardening
  added a retry-enabled SQLite creator (4 attempts, busy timeout
  inside the creator).
- Migration #7 (`c3f81b92ad04`); `test_tier1.py` 27 checks; 19/19 +
  flake8 green. Docs updated (README, ROADMAP Tier 1 SHIPPED,
  SECURITY_AUDIT, env example).

## Phase 4 — System fixes (the audit round)

- **Bank numbers encrypted at rest:** Fernet ciphertext in
  `bank_account_number` (String 512), `bank_account_last4` for masked
  display (no decryption), `security_crypto.py` + `BANK_ENC_KEY`
  (SECRET_KEY-derived fallback), legacy plaintext tolerated +
  re-encrypted on save, migration #8 (`e91c73a40b57`). Sole
  decryption surface = bank CSV. (`cryptography==44.0.1` pinned.)
- **Revision-loop cap:** `TASK_MAX_REJECTIONS=3`; the 3rd rejection of
  a task force-returns it to the pool, notifies the annotator
  (`task.escalated`), and records escalation in flash + audit detail.
- **Vision exports:** COCO JSON (schema-valid; tasks→images with
  `brydl_text`; labels→categories; never carries gold) and YOLO text
  ZIP (sorted classes.txt, 5-column full-frame placeholder lines —
  the test caught the first writer emitting bare class indexes —
  README + manifest in-zip).
- **Regression caught by the battery:** masked-display refactor
  rendered `****` for no-bank annotators (empty-string truthiness);
  user_mgmt suite caught it; fixed to "Not provided".
- **Config factory fix:** the SQLite creator originally hardcoded the
  default DB path, breaking any engine pointed elsewhere — refactored
  to a per-URI creator factory (migration suite caught it).
- `test_system_fixes.py` 30 checks; **20/20 suites, 429 checks,
  flake8 clean**.

## Standing decisions & deviations (with reasons)

| Decision | Why |
|---|---|
| Self-hosted Bootstrap instead of CDN | Production login outage; offline-first Nigeria reality |
| Fail-closed user_loader | A DB hiccup must never elevate privileges; silent-anon is the acceptable failure |
| Creator-instead-of-connect_args on SQLite | connect_args are ignored when a creator exists; busy timeout must live in the creator |
| Migration #8 widens but does NOT rewrite rows | The encryption key lives in app env, not migrations; legacy rows read through gracefully |
| Reject-cap counts ALL rejected submissions of the task | Survives annotator changes; a poisoned task escapes any pairing |
| COCO/YOLO ship placeholders before media tasks | Clients can validate pipeline wiring now; geometry lands with media tasks without format changes |
| Human review of every submission (no auto-approve yet) | Quality moat at small scale; earned-trust automation deferred to Tier 2/3 |

## 2026-09-18 — Blueprint + repository hygiene

- blueprint/ specification created (13 files, update protocol in its
  README): rebuildable from first prompt to current state.
- Repo visibility verified PRIVATE via anonymous fetch; security
  posture documented in blueprint 06 (2FA GitHub+Render, unique
  passwords, no secrets in repo ever).

## 2026-09-18 — RLHF Phase 1: preference (DPO) task type

- New task type `preference`: prompt + Response A/B side-by-side
  workspace, chosen radio + required written justification
  (PREFERENCE_MIN_JUSTIFICATION=40).
- Position-bias defense: deterministic per-annotator A/B flip
  (blake2b of task_id:annotator_id) computed server-side on GET and
  POST; submissions store the CANONICAL choice + seen_order.
- Gold isolation holds; quality gold accuracy compares `chosen`;
  review detail renders canonical columns + justification.
- Import: both paths accept JSON preference lines; gold normalized
  at preview AND confirm (the confirm-time check closed a real gap).
- Suite: test_rlhf_preference.py (23 checks); 21/21 green, flake8
  clean. Build bug caught by the suite: the form-builder branch was
  initially appended AFTER the catch-all else, silently adding a
  required answer_text to preference forms.

## 2026-09-20 — RLHF Phase 2: blind consensus + arbitration

- Migration #9 (`b2f4c9d61a83`): consensus_groups table
  (ck_consensus_status), tasks.consensus_group_id FK, projects.
  required_annotations (ck_projects_consensus_depth, 1-3).
- Grouped import on BOTH paths: depth >1 turns each row into a group
  of identical siblings; claims + admin assigns refuse a second
  sibling of a held group (blindness structural + tested).
- consensus.py: verdict keys (preference chosen / classification
  label); evaluation on submit + verdicts; a rejected sibling
  withdraws its verdict (round → pending); resolved groups frozen.
- Arbitration UI (`/admin/review/conflicts`): evidence list, per-round
  view with canonical choices + justifications, binding ruling
  (audited `consensus.resolve`, re-arbitration refused).
- Exports: JSONL consensus block; CSV consensus_status/decision cols.
- Suite test_consensus.py (32 checks). Battery reconciliation: project
  POSTs in 4 suites gained the depth field; export header extended;
  migration normalizer now strips named-FK prefixes (semantic no-op).
  22/22 green, 565 checks, flake8 clean.
- Probe insight worth keeping: two annotators choosing the SAME
  displayed letter can be choosing DIFFERENT canonical responses when
  their position flips differ — canonical mapping + seen_order is what
  makes consensus statistically meaningful.

## 2026-09-20 — Ops round closed (operator-reported)

- DB credential rotation completed on Render (new default credential,
  service switched, old credential deleted).
- 2FA enabled on GitHub and Render; recovery codes stored offline.
- SMTP verified end-to-end: reset-link email received on the live
  site; notification emails (assigned/rejected/escalated/paid) are
  now LIVE transports, not just in-app rows.
- UptimeRobot monitor + weekly pg_dump routine adopted.
- render.yaml region lines removed in-repo (rides the next push).

## 2026-09-24 — RLHF Phase 3: trusted rendering

- rendering.py: markdown (+fenced code, tables, nl2br) → nh3
  allowlist sanitizer → Pygments-class highlighting; conversation
  JSON → chat bubbles; oversize/invalid content degrades to escaped
  plain text; the module never raises. Self-hosted
  static/css/pygments.css (no CDN, no client-side JS).
- Applied to annotator workspace input + preference prompt/responses,
  review-detail input, arbitration prompt. Storage + exports stay raw.
- config: MARKDOWN_RENDER_MAX_CHARS=20000.
- **Battery catch (real bug):** an earlier splice dropped a success-
  path `return _render_task_page(...)` ahead of the redirect —
  submissions SAVED but annotators saw the form again and consensus
  rounds never re-evaluated (stress60, consensus, preference and
  annotator suites all flagged it). Fixed: the re-render belongs to
  the except path only.
- system-check `|safe` policy modernized: allowed ONLY on
  sanitizer-backed roots (input_view/prompt_view/pref_views/turn);
  enforcement strips non-word prefix chars before root extraction.
- 23/23 suites, flake8 clean (checks count later corrected — see
  the Phase 4 entry).

## 2026-09-24 — RLHF Phase 4: SFT gold-rewrite builder

- sft.py: pure harvest pipeline (unwrap output docs per kind, map
  preference choices back to winning response text, conversation
  last-user-turn prompts, harvest priority consensus > approved >
  gold, chat-messages export shape). Never raises; malformed rows
  degrade to None/manual.
- New table sft_examples (migration c4e8a1f37d52): prompt +
  source_completion (immutable harvest anchor) + editable completion,
  mandatory edit note on every rewrite, draft → ready → exported
  lifecycle (exported terminal + frozen), ck_sft_status /
  ck_sft_source checks, task/project/status indexes.
- Admin surface /admin/sft (5 routes, 2 templates, nav entry):
  filtered list with status counts, harvest-by-task-id prefill,
  create/edit with rendered preview, draft-only delete, POST-only
  JSONL export that freezes exported pairs in the same transaction
  as its audit event. Full audit trail: sft.create/update/delete/
  export (entity_id rides the caller's transaction — log BEFORE
  commit).
- Battery catches (all real): (1) @admin_required must sit BELOW
  @admin_bp.route — inverted order registers the UNWRAPPED view and
  silently drops the role wall (new SFT routes were open to
  annotators until the system-check matrix flagged the POSTs);
  (2) sft_delete originally ignored its CSRF token — caught by the
  same matrix; (3) log_event after commit() loses the audit row.
- test_sft.py: 53 checks. **24/24 suites, 570 checks, flake8 clean**
  (570 is from a clean scratch tally; the earlier "615" for Phase 3
  double-counted stray debug logs — true 23-suite figure ~517).

## 2026-09-25 — RLHF Phase 5: rubric dimensions + sliders

- rubric.py (pure): dimension-list parsing (strip/order/dedupe/
  caps) + strict score validation (exact dimension set, exactly
  {"a","b"} ints in scale range, booleans rejected).
- projects.rubric_dimensions (migration d7f2b9e46a10, nullable
  Text, one dimension per line) configured on the project form with
  server-side problems flashed; NULL = rubric off.
- Preference workspace: NATIVE range sliders (zero client JS —
  offline rule intact), 2 fields per dimension, named by INDEX and
  mapped to the CANONICAL response exactly like the choice radios
  (display side is randomized; canonical mapping is server-side).
  Scores stored in the output document as {"rubric": …}.
- Review detail: rubric table; arbitration: rubric badges per
  sibling — rendered only when the stored rubric validates strictly
  against the project's dimensions. Consensus verdict keys
  unchanged. JSONL export carries the rubric inside the output
  document (CSV header unchanged).
- Battery catches (test bugs, not product bugs): tamper tests must
  target a RUBRIC project task; directly-inserted sibling tasks need
  an explicit ConsensusGroup (groups are normally import-born); an
  export JSONL check requires an APPROVED submission (export scope).
  Also: _render_task_page call sites must pass every new kwarg —
  the GET page silently dropped sliders until the call sites were
  updated.
- test_rubric.py: 37 checks. **25/25 suites, 607 checks, flake8
  clean.**

## 2026-09-25 — Phase 6: QA + auditor staff roles; landing rewrite

- Two new staff roles: `qa` (review queue, review detail, bulk
  review, quality) and `auditor` (consensus conflicts, binding
  arbitration, audit trail). New shared decorators in routes/__init__:
  @qa_required = role_required("admin","qa"),
  @auditor_required = role_required("admin","auditor"). Admins keep
  full access; financial surfaces stay admin-only.
- ck_users_role widened via migration e5c7d2f84b19 (check constraint
  recreated; existing rows satisfy the new vocabulary). Team page
  offers all four roles. Sign-in routes each role to its queue;
  `/` redirects qa/auditor from the financial dashboard.
- Navigation is role-scoped (QA sees QA Review + Quality; auditor
  sees Arbitration + Audit Trail) and the header role badge covers
  all four roles.
- Landing page rewritten for a dual audience: "Africa's AI data
  labelling company" — clients section (vision/text/audio, RLHF +
  rubric, auditable quality, JSONL/CSV/COCO/YOLO delivery, African
  languages) and staff section (exact per-task pay, low-bandwidth
  workspace, QA feedback, growth into QA/auditor roles).
- system-check security matrix now accepts the three named staff
  decorators (role wall can never silently disappear again).
- Battery catch: test_brand pinned the old hero copy — needles
  updated alongside the rewrite. test_roles caught the arbitrate
  form's real field names (decided_choice/decided_note) and the
  /users/create path during bring-up.
- test_roles.py: 48 checks. **26/26 suites, 655 checks, flake8
  clean.**

## 2026-09-25 — Tier 2: Paystack Transfers + NDPR page

- paystack.py: stdlib-HTTP client (no new dependencies), every call
  returns (ok, payload) and never raises; secret never logged.
  Recipient creation (NUBAN), NGN bank list + fuzzy bank matching,
  transfer initiation in kobo, HMAC-SHA512 webhook verification.
- Payout route (admin-only, POST + CSRF): decrypts bank details
  in-request only, creates + caches the recipient, converts USD ->
  NGN -> kobo at one rounded Decimal boundary, stores the
  idempotency reference, flips to paid only on provider-confirmed
  success. Disabled key = CSV batch flow untouched.
- Webhook (public): signature-gated, settles one pending payment by
  reference; retries idempotent. Audit trail covers initiation,
  failure and settlement — never an account number.
- NDPR page at /privacy (public, footer link): controller/processor
  split, collection, lawful basis, sharing, security, retention,
  rights.
- Migration f1a94c3b7e20 (chain 13): users.paystack_recipient_code,
  payments.paystack_reference + index.
- Battery catch: the CSRF matrix flagged the unsigned-by-design
  webhook — resolved with an explicit documented exemption list, not
  by weakening the rule.
- test_paystack.py: 36 checks. **27/27 suites, 691 checks, flake8
  clean.**

## 2026-09-25 — Phase 7: client portal (dashboard → company app)

- New surface: /client/<token> — a client follows their project with
  an opaque link (no account): progress %, status counts, average QA
  score, and self-serve downloads of APPROVED rows as client-safe
  JSONL/CSV (no annotator identity, no gold markers, no reviewer
  notes).
- Threat model: the token IS the credential (32-byte urlsafe, only
  sha256 stored, dead links 404 without advertising existence), the
  raw token is redacted from REQ access logs, never reflected into
  page text (only the export hrefs carry it), never audited, pages
  are noindex, and admins revoke instantly (audited).
- Admin UI on the project export page: issue (label only, raw link
  flashed once — the temp-password contract) and revoke.
- Battery catches: the decorator-order trap (@admin_required BELOW
  @admin_bp.route) bit twice more — the security matrix flagged the
  unprotected POSTs both times; a bad test expectation (token must
  legitimately appear in export hrefs) was corrected to assert
  href-only presence.
- Migration a9b3d7c15e40 (chain 14). test_client_portal.py: 43
  checks. **28/28 suites, 734 checks, flake8 clean.**

### Phase 8 — Media workspaces (image labelling + audio transcription)

- `MediaAsset` model (migration `b8e40c92f7d1`, chain 15): project FK,
  kind ck (image|audio), sniffed content_type, filename, byte_size ck,
  sha256, data **LargeBinary (documented deviation)** — bytes live
  in-row so they ride every pg_dump/backup and survive redeploys at
  $0; an object-store (R2) move later is additive (columns only, no
  data rewrite of existing rows' semantics).
- Upload pipeline (admin-only media library on
  `/admin/projects/<id>/media`, linked from the project export page):
  MEDIA_MAX_BYTES cap (8 MB knob), **magic-byte sniffing** (`_sniff_media`:
  PNG/JPEG/GIF/WebP + WAVE via RIFF disambiguation + OggS + MP3
  ID3/frame-sync + ftyp→audio/mp4; **SVG never accepted**), sha256
  per-project dedupe (unique index reuses the row), audited
  `media.upload` (sha256 prefix only).
- Task wiring: import docs reference media via
  `{"kind": "image_labeling"|"audio_transcription", "media_id": N,
  "prompt"?, "labels"?}`. Annotator workspace renders the image (or
  audio player) + sanitized prompt; image answers = label radios from
  the doc's labels (free-text fallback) + optional notes; audio =
  required transcript. Serving is **task-bound**:
  `/annotator/task/<tid>/media/<aid>` 404s unless the task's doc
  references the asset, the asset is in the task's project and the
  task belongs to the caller — ids cannot be probed. Staff route
  `/admin/media/<id>` for admin/qa/auditor.
- QA review + arbitration pages render the same media (admin-side
  parser), so conflicts are judged on the actual image/audio.
- Bug the battery caught: passing media labels through the form
  builder's rubric_dims slot fabricated invisible `rubric_N_a/b`
  InputRequired sliders that bricked every labelled image submission —
  fixed with an explicit `label_choices` parameter. Also pinned:
  WTForms ≥3.2 reduces `FileField.data` to a str — media bytes are
  read from `request.files` (form still owns CSRF + presence).
- Migration b8e40c92f7d1 (chain 15). test_media.py: 39 checks.
  **29/29 suites, 773 checks, flake8 clean.**

### Phase 9 — Qualification quizzes (per-project gate)

- `quizzes.py` (pure): paste-format parser (`Q:` question blocks,
  `- ` wrong / `* ` correct options — exactly one correct, ≥2
  options, ≤30 questions, length caps), canonical JSON payload,
  server-side grading, integer score rounding, and the deterministic
  per-(quiz, annotator) blake2b display order (same contract as the
  RLHF flip — a refresh never reshuffles a half-answered attempt).
- Models (migration `e2f6a9c4d8b1`, chain 16):
  `qualification_quizzes` (UNIQUE project_id; draft → active →
  retired; pass_score 1-100 ck; max_attempts 1-20 ck) +
  `quiz_attempts` (passed/failed/exempt ck; canonical answer sheet;
  granted_by for exemptions; composite index for gate + counter).
- Admin quiz console (`/admin/projects/<id>/quiz`, linked from the
  project export page): author/edit while DRAFT only (an active quiz
  never changes under mid-attempt annotators — retire, edit,
  reactivate), activate (notifies every active annotator, audited),
  retire, grant/revoke audited exemptions, attempts table, qualified
  roster.
- The gate: claim skips gated projects (falling through to free
  work), assignment reports gated tasks as skipped with a warning
  (grant an exemption when staff decide to proceed), and the task
  page redirects the annotator to the quiz. Draft/retired gate
  nothing.
- Annotator surface: dashboard "Qualification needed" card with
  attempts used, quiz page (deterministic order, options-only
  render), honest scoring flash with attempts left, terminal pass,
  exhausted-attempt refusal pointing to support.
- Security posture: the correct-answer key NEVER leaves the server
  (choices are texts; grading is server-side); CSP hygiene kept (the
  battery's inline-handler scan caught my onclick confirms —
  removed).
- Seed: a DRAFT demo quiz ships with the sample project (non-gating
  until deliberately activated).
- Migration e2f6a9c4d8b1 (chain 16). test_qualification.py: 52
  checks. **30/30 suites, 825 checks, flake8 clean.**

### Phase 10 — API tier + background workers (DB-backed queue, Redis optional)

- `api_keys.py` — CSPRNG key generation + sha256 hash, recognisable
  `brydl_` prefix.
- `routes/api.py` (`api_bp`, `/api/v1`): Bearer-key auth (no
  cookies/CSRF surface by construction), project-scoped (foreign
  keys are uniform 404s), per-key rate limit via `kvstore`. Endpoints:
  `/ping` (open uptime probe), `/me`, `/projects/<id>` (progress),
  `/projects/<id>/results` (paginated client-safe rows, same
  contract as the portal export — no annotator identity, no gold,
  no reviewer notes), `/projects/<id>/tasks` (POST batch push,
  fail-closed validation, 500-cap, audit counts only).
- Admin surfaces (`routes/admin.py`, linked from the project export
  page): ApiKeyForm, `/projects/<id>/api-keys` issue (show-once raw
  key in flash, sha256 only persisted, audit prefix-only),
  `/api-keys/<id>/revoke` (CSRF, instant 401, audit).
- `kvstore.py` — optional Redis client (lazy, ping-probed,
  failure-isolated) with an in-process fallback so deployment can
  start without any second infrastructure credential.
- `jobs.py` — DB-backed queue:
  - `enqueue` (best-effort, never blocks request path)
  - `claim_job` via conditional UPDATE (queued→running) so N
    gunicorn workers or a dedicated `worker.py` share the table
  - exponential backoff (30s → 1h, capped 3600s), terminal failure
    after `max_attempts` with `job.failed` audit; unknown kinds
    park immediately (no retry)
  - built-in runners: `email.deliver` (SMTP leg off the request
    path; in-app notification row is the durable copy so a queued
    send never loses a notification) and `paystack.reconcile` (one
    sweep over pending payouts older than the cutoff; verified
    success settles exactly like the webhook, explicit failure
    marks the payment failed, pending-guard keeps re-sweeps
    idempotent)
  - `_requeue_stuck` heals rows left `running` by a killed process
    on boot
  - `start_runner` daemon thread; `JOBS_ENABLED` and
    `BRYDL_DISABLE_JOBS` kill switches; `worker.py` standalone
    entrypoint provided for future scale
- `notifications.py` — SMTP leg queued when the runner is live.
- Migration `c7d21b8a45e9` (chain 17): api_tokens + background_jobs,
  backfill `payments.created_at` (COALESCE paid_at/period_end) for
  the reconcile cutoff.
- Carried gap (⑧) closed: `consensus.verdict_key` now branches on
  `image_labeling` (same `label` → agreed, bbox geometry ignored)
  and `audio_transcription` (always `None` — free-text transcripts
  are arbitrated, never auto-agreed by coincidence).
- test_api.py 34 checks, test_jobs.py 20 checks, plus 2 carried
  consensus checks. **32/32 suites, 881 checks, flake8 clean.**

## Open items (not done on purpose)

- SMTP end-to-end test (operator deferred).
- DB password rotation (recommended, operator-side).
- render.yaml `region: fra` cleanup on next touch.
- UptimeRobot + weekly pg_dump (operator-side, recommended).
- Avatar upload (offered, not approved; ~256 px Pillow, ≤2 MB).
- Tier 2: Paystack Transfers + CSV fallback, client token portal,
  NDPR page. Tier 3: media tasks, consensus, live FX, avatars.
- RLHF direction agreed (preference comparison → consensus/arbitration
  → trusted rendering → SFT editing → rubric dims) — awaiting start.

## 2026-10-08 — v13.1 Hausa special-character toolbar

Annotator-requested UX patch (Abdul Muhammad): a clickable Hausa
special-character toolbar is injected above every annotator textarea
(ɗ ƙ ɓ ƴ ʼ Ɗ Ƙ Ɓ Ƴ). Clicking a char inserts it at the caret, falls
back to append when nothing is focused. Zero inline handlers (strict
CSP preserved); scoped `.special-char-bar` / `.char-btn` CSS.
Brydl logo asset added at `static/img/brydl-logo.jpg` (navy Africa
outline + binary + "Brydl" wordmark). 42/42 suites, flake8 clean,
zero migrations (JS/CSS-only). Tagged `v13.1` on `dc9f25f`.

## 2026-10-08 — v14 RedLab Phase 1: ASGI host + healthcheck

Second product line (dynamic AI red-teaming as a service) scaffolded
alongside data labeling. Single-repo / single-deploy architecture:
FastAPI wraps Flask via a2wsgi.WSGIMiddleware in a new root `asgi.py`
so RedLab's async /redlab/* routes and Flask's sync routes coexist on
one gunicorn + UvicornWorker process. Phase 1 scope:

- New deps: `fastapi==0.115.12`, `uvicorn[standard]==0.34.2`,
  `httpx==0.28.1`, `pydantic==2.11.3`, `a2wsgi==1.10.8`,
  `import-linter==2.2` (cryptography 50.0.1 already present).
- `redlab/` package: `__init__.py` (import contract), `flags.py`
  (`is_redlab_enabled()` — case-insensitive exact match on `"true"`),
  `app.py` (FastAPI sub-app with `/healthz`, JSON-500 error boundary
  middleware stamping X-Request-ID, gated `/v1/*` mount that returns
  `{"error":"redlab_disabled"}` while flag OFF and `{"error":"not_found"}`
  when ON but no route registered).
- Root `asgi.py` composer: mounts `/redlab` → redlab.app, `/` → Flask
  via WSGIMiddleware. Startup event raises AnyIO's to-thread limiter
  to 60 tokens so Flask handlers under the WSGI→ASGI bridge can absorb
  ~50 concurrent annotators × 2 workers on the free tier. Falls back
  to pure-Flask WSGI (degraded mode) if FastAPI/a2wsgi are missing.
- `render.yaml` + `Procfile` updated: `gunicorn -k
  uvicorn.workers.UvicornWorker --workers 2 asgi:app`; health check
  switched to `/redlab/healthz` (returns 200 in both flag states so
  the platform keeps serving during the dark rollout).
- Feature flag `REDLAB_ENABLED=false` DEFAULT — the entire /redlab/v1
  namespace is dark until Phase 9. Existing Flask behavior is
  byte-identical in the OFF state.
- Tests: `tests/test_phase1_asgi.py` with 31 assertions (deps, flag
  strictness, healthz in both states, 5 HTTP methods gated at /v1,
  Flask routes via WSGI bridge including auth-redirect and static
  404, JSON-500 error boundary with message-not-leaking sanity,
  threadpool ≥ 60, flag-on 404s return JSON not HTML). Updated
  `tests/test_deploy.py` and `tests/test_scalability.py` to reflect
  the new ASGI start command instead of the old `--threads 4
  "app:create_app()"`.
- Design doc at `docs/v14-REDLAB-DESIGN.md`.
- 43/43 suites green (42 prior + test_phase1_asgi.py); flake8 clean
  across redlab/, asgi.py, and touched tests. No migrations (no
  schema changes in Phase 1).

Branch `v14-redlab`. Per LOCKED Phase 0 rule #7, work pauses here for
explicit operator confirmation before Phase 2 (migration #18,
redlab.models, redlab.crypto).

## 2026-10-08 — v14 RedLab Phase 2: Migration #18 + models + Fernet crypto

- `redlab/crypto.py`: Fernet (AES-128-CBC+HMAC) helpers encrypting client
  LLM API keys at rest. `REDLAB_FERNET_KEY` env var with SECRET_KEY-derived
  dev fallback; `SecretDecryptionError` never leaks key material in
  exception text; `mask_key_fingerprint("sk-abc...0001")` →
  `"sk...0001"` for UI display.
- `redlab/models.py`: four tables with UUID-hex PKs in Postgres schema
  `redlab` (default schema on SQLite where schemas aren't supported):
  Target (endpoint + Fernet key + key_fingerprint + ssrf_validated_at),
  Session (target_id/annotator_ref/project_ref, status open|concluded,
  turn_count/breach_count counters, submission_ref UNIQUE for the
  Phase-5 conclude link), Message (session_id FK ON DELETE CASCADE,
  turn_index UNIQUE per session, role user|assistant|system, client_turn_id
  idempotency key, is_unsafe_flag, redacted), ProcessedEvent (event_id
  PK dedup table for Phase 5 events).
- Additive columns on public tables (declared directly in models.py):
  `submissions.redlab_session_id` (UNIQUE nullable),
  `submissions.redteam_state` (default 'none'),
  `submissions.redteam_breach_count` (default 0);
  `projects.redteam_base_pay`, `redteam_breach_bounty` (Numeric(10,2)
  default 0.00), `redteam_session_quota` (int default 0).
- Migration #18 (`a1b2c3d4e5f6`): creates schema `redlab` (Postgres),
  four tables (FKs, CHECKs, UniqueConstraints, server_defaults using
  sa.text("0")/("1")/("0.00") for SQLite/Postgres parity), six
  additive columns via batch_alter_table (SQLite-portable); downgrade
  drops in reverse order and `DROP SCHEMA redlab CASCADE` on Postgres.
- app.py imports redlab.models inside create_app so SQLAlchemy metadata
  is registered (for create_all/Alembic autogenerate). redlab.models
  imports zero Flask routes/templates/business logic — import-linter
  will enforce this contract in Phase 9.
- tests/test_phase2_models.py: 48 checks across three subprocess-isolated
  sections — crypto roundtrips + error-leak safety + fingerprint;
  migration upgrade/downgrade on temp SQLite; CRUD exercising defaults,
  UNIQUE/(session_id,turn_index), ProcessedEvent dedup, cascade delete,
  and stored-ciphertext roundtrip.
- tests/test_migrations.py normaliser extended to strip named UNIQUE/CHECK
  constraint prefixes that batch_alter_table emits.

Branch `v14-redlab`, commit d7c233c. 44/44 suites green; flake8 clean
on new/modified code (pre-existing E501s untouched); migrations
single-head. Flag still OFF. Per LOCKED Phase 0 rule #7, paused at
Phase 2 gate awaiting explicit go before Phase 3 (async connector,
SSRF defense, mock model).

## Phase 3 — SSRF guard, mock model, async client connector (2026-10-08)

Files added:
- `redlab/ssrf.py` — `validate_endpoint_url(url) -> (bool, reason)`:
  https-only (http://loopback only with `REDLAB_ALLOW_HTTP_LOCAL=1`),
  blocks loopback 127.0.0.0/8, RFC1918 10/8 + 172.16/12 + 192.168/16,
  link-local/metadata 169.254/16, CGNAT 100.64/10, IPv6 ::1,
  fc00::/7, fe80::/10. Does real `socket.getaddrinfo` resolution
  for hostnames so DNS-rebinding attacks are caught; unparseable
  IPs are blocked by default.
- `redlab/mockmodel.py` — `mock_respond(history, seed_modifier)`:
  deterministic offline mock using md5 seeding; ~45% canary-leak
  rate on credential probes (`SK-CANARY-7F3`); returns `[BYPASS]`
  marker on DAN/jailbreak keywords; per-language Yoruba / Hausa /
  Swahili / French signatures for language-autodetect tests.
- `redlab/connector.py` — `build_history(messages)` and
  `call_client_model(target, history)` using `httpx.AsyncClient`
  with connect=5s/read=20s timeout, one retry with 1s backoff on
  429/5xx/ConnectError/TimeoutException, in-memory Fernet decrypt
  of stored `api_key_ciphertext` for the Bearer header, error
  taxonomy (`endpoint_blocked`, `client_auth_failed`,
  `client_rate_limited`, `client_unreachable`, `client_bad_response`),
  and regex redaction of `sk-[A-Za-z0-9_-]{20,}` and `Bearer ...`
  tokens in every error/success string leaving the function.
- `tests/test_phase3_connector.py` — 46 table-driven checks: 17
  SSRF cases, 15 mock-model cases (seed consistency, canary leak
  rate ~45%, jailbreak [BYPASS], Yoruba/Hausa/Swahili/French
  language markers), 14 connector cases (history build, SSRF
  short-circuit, 200 parse, auth header, endpoint URL, 401→auth,
  429→rate_limited+retry, 500 retry-succeeds, ConnectTimeout→unreachable,
  sk/Bearer redaction of error bodies, 5xx exhaustion→bad_response).

Test endpoints use `httpx.MockTransport` with a public-IP literal
(93.184.216.34) so SSRF's real DNS resolution doesn't reject test
traffic; SSRF cases themselves use `https://` + IP-literal hosts so
hostname-block fires after scheme check. httpx 0.28 compatibility:
`httpx.Timeout(5.0, connect=…, read=…)` (requires default or all
four params).

Branch `v14-redlab`, Phase 3 commit. 45/45 suites green
(test_phase3_connector.py picked up by run_all.sh); flake8 clean
on new files; REDLAB_SSO_CONFIGURED flag still OFF. Per LOCKED
Phase 0 rule #7, paused at Phase 3 gate awaiting explicit go
before Phase 4 (routes, UI, integration).

## Phase 4 — Turn API & session-cookie auth (2026-10-09)

Files added/changed:
- `redlab/schemas.py` — Pydantic v2 request/response models: TurnRequest
  (content ≤4000 chars, client_turn_id ≤64 chars), MessageOut,
  SessionOut, TurnResponse, SessionResponse, FlagRequest, ErrorResponse.
- `redlab/auth.py` — FastAPI Depends():
  * `get_current_annotator`: reads Flask's signed `session` cookie
    (itsdangerous URLSafeTimedSerializer, salt="cookie-session",
    SECRET_KEY), validates signature + 30-day age, loads User via
    Flask-SQLAlchemy (pushing an app context from the worker thread),
    rejects inactive / non-redteam roles.
  * `verify_csrf_header`: decodes the `X-CSRFToken` header using
    flask-wtf's salt ("wtf-csrf-token", 1-hour TTL), hmac.compare_digest
    against the raw `session["csrf_token"]`.
  * Shared `_flask_app()` cached factory used by both auth and the
    turn route so SQLAlchemy sessions resolve the same Flask app
    across the FastAPI→thread boundary.
- `redlab/app.py` — replaced the `/v1/*` gate with a real mounted
  APIRouter:
  * `POST /sessions/{sid}/turn` enforces session ownership, status=open,
    40-turn cap, idempotency via `client_turn_id` (cached early-out
    before the connector call AND a double-check under the write
    lock against raced concurrent retries), per-message turn_index
    (user at 2*N, assistant at 2*N+1) preserving the UNIQUE
    (session_id,turn_index) constraint, atomic commit of user+
    assistant rows + turn_count bump, zero-row write + rollback on
    any connector failure, structured 502/503 mapping by error code.
  * JSON exception handlers for RedLabAuthError / HTTPException that
    always emit `{error, detail, request_id}`.
  * `_flask_ctx()` re-entrant context manager so the async route can
    run sync SQLAlchemy safely from AnyIO worker threads.
- `tests/test_phase4_turn_api.py` — 50 checks across 6 sections:
  auth (16 cases: no cookie/bad cookie/no CSRF/bad CSRF/inactive/
  other-owner/unknown-sid/concluded), happy path (13: 200, body
  shape, client_turn_id echoed, turn_count=1, connector-called-once,
  2 DB rows with correct turn indices 0/1), turn cap (41st → 400,
  connector not called, no new rows), idempotency (retry returns
  cached ids/content, connector not called, turn_count/rows
  unchanged), atomicity on failure (unreachable → 502, rate_limited
  → 503, zero rows written, turn_count unchanged), oversize → 422.

Lessons (recorded for future phases):
- FastAPI routes mounted alongside Flask via WSGIMiddleware run on
  AnyIO worker threads with no Flask request/app context; DB access
  needs an explicit `app.app_context().push()` around every sync
  block. A single cached `_flask_app()` factory is required so the
  SQLAlchemy registry (bound to the first `db.init_app(app)`) is the
  same instance used by both setup and requests — a second
  `create_app()` call produces a separate engine whose DB is empty.
- flask-wtf stores a RAW random token in `session["csrf_token"]`;
  the client receives a SIGNED token (URLSafeTimedSerializer,
  salt="wtf-csrf-token", 1-hour TTL). CSRF validation must decode
  the signed header and compare against the raw session value, not
  the other way round.
- `Message.turn_index` is per-message (user at 2*N, assistant at
  2*N+1); `Session.turn_count` counts completed pairs. Sharing an
  index across both roles violates the UNIQUE (session_id,
  turn_index) constraint.

Branch `v14-redlab`, Phase 4 commit. 46/46 suites green
(test_phase4_turn_api picked up by run_all.sh); flake8 clean on
new/modified files. Per LOCKED Phase 0 rule #7, paused at Phase 4
gate awaiting explicit go before Phase 5.
