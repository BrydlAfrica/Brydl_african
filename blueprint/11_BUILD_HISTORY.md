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
