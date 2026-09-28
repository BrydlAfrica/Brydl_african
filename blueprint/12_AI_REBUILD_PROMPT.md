# 12 — AI rebuild prompt

Paste everything below into an AI coding agent to rebuild Brydl from
zero. It is the compressed contract; the other files in this folder
hold the detail. Work through it top to bottom and accept nothing as
"done" without its gate.

---

## ROLE

You are building **Brydl**, a production micro-task annotation
platform, for a solo CTO in Nigeria. Build complete, production-ready
code only — no TODOs, no placeholders, no "minimal examples".

## STACK (do not deviate)

- Python 3.10+ (deploy on 3.13), Flask 3.x app-factory pattern,
  Jinja2, SQLAlchemy ORM only (no raw SQL), Flask-Migrate (Alembic),
  Flask-Login, Werkzeug password hashing, Flask-WTF (CSRF on every
  POST), Gunicorn, Render + PostgreSQL production / SQLite dev+tests.
- Bootstrap 5.3.8 + Bootstrap Icons 1.13.1 **self-hosted** in
  static/vendor/ (download the dist files; NO CDN, NO npm).
- PEP 8 + docstrings everywhere; flake8 clean is a gate.

## PRODUCT

Two roles. **Admin** (the operator): creates projects (name, client,
task_type, pay_per_task $0.05–$5.00 USD Decimal, deadline, status
active/paused, instructions), imports tasks via CSV (paste or file →
validated preview ≤2000 rows/2 MB → confirm; template download),
assigns tasks, reviews submissions (approve/reject, score 0–100,
notes ≤2000, optional return-for-revision), manages payouts
(generate runs → mark paid → bank CSV), manages users (create,
toggle-active, temp-password reset), views a quality dashboard
(per-annotator approval rate, avg QA score, gold accuracy) and an
audit trail, and exports project data (JSONL, CSV, COCO JSON, YOLO
ZIP). **Annotators**: sign in, accept terms (versioned gate),
self-claim the oldest pending task (20/day cap, race-safe), work
tasks in a type-driven workspace, see their history and QA notes,
get notifications (bell + feed: assigned/rejected/escalated/paid),
and manage profile/password/bank details. Landing page + branded
403/404/405/500. Light/dark theme per user. PWA (manifest, service
worker, offline page).

## DATA MODEL (single models.py; UTC via utc_now(); money via money())

users(id, name, email unique, password_hash, role CHECK admin|annotator
default annotator, is_active, created_at, terms_version,
terms_accepted_at, theme, reset_token_hash indexed, reset_token_expires,
bank_name, **bank_account_number = FERNET CIPHERTEXT String(512)**,
bank_account_last4, bank_account_name, bank_account_type, phone,
auth_version default 0) · projects(id, name, client_name, task_type,
pay_per_task Numeric(10,2), deadline Date, instructions, status CHECK
active|paused|completed indexed, created_by FK, created_at) · tasks(id,
project_id FK indexed, input_data JSON-text, expected_output JSON-text
NULLABLE = GOLD (admin-only, NEVER rendered/selected by annotator
routes), status CHECK pending|assigned|submitted|approved|rejected
indexed, claimed_at nullable, assigned_to FK NULL⇔pending
(DB CHECK ck_tasks_assignment), created_at) · submissions(id, task_id
FK indexed, annotator_id FK indexed, output_data JSON-text, qa_score
Float, qa_notes, status CHECK submitted|approved|rejected indexed,
submitted_at, reviewed_at, payment_id FK nullable) · payments(id,
annotator_id, amount Numeric(10,2), currency default USD,
period_start, period_end, tasks_count, status CHECK pending|paid
indexed, payment_method, paid_at) · audit_logs(id, user_id, actor_name,
action indexed, entity_type, entity_id, detail JSON-text = counts
NEVER data, ip_address, user_agent, created_at indexed; append-only) ·
notifications(id, user_id indexed, kind CHECK
task.assigned|task.rejected|task.escalated|payout.paid, payload,
read_at, created_at; composite indexes (user_id,read_at) and
(user_id,created_at)).

## CORE CONTRACTS

1. **Claim race-safety:** POST claim → CSRF → daily cap (tasks with
   claimed_at today) → retry loop (3×): pick oldest pending in oldest
   active project, conditional UPDATE
   `WHERE id=:picked AND status='pending' SET assigned_to=:me,
   status='assigned', claimed_at=now()`; rowcount 0 → sleep
   0.05·(n+1) → retry. Audit task.claim. 9 parallel claims MUST yield
   9 distinct winners (test it).
2. **Review + revision cap:** verdicts apply conditionally
   (status='submitted'). Reject+keep → task 'rejected' + task.rejected
   notification. Reject w/o keep → task pending unowned. If a task's
   TOTAL rejected submissions (any annotator) ≥ TASK_MAX_REJECTIONS
   (3) → force-return to pool + task.escalated notification + audit
   escalation count.
3. **Bank encryption:** security_crypto.py Fernet (cryptography
   pinned); key BANK_ENC_KEY else SHA-256(SECRET_KEY)-derived;
   set_bank_account() encrypts + stores last4; get_bank_account_number()
   decrypts (legacy plaintext tolerated, re-encrypt on save);
   Team page shows last4 or "Not provided" and NEVER decrypts; the
   bank-batch CSV is the only decryption surface; audited with counts.
4. **Exports:** admin + CSRF + scope(approved/reviewed/all) + gold
   only-if-ticked (COCO/YOLO never carry gold) + yield_per streaming +
   audit(format,scope,counts). COCO: images=task_<id>.txt w=0 h=0 +
   brydl_text, annotations w/ category from output label, categories
   sorted from 1. YOLO ZIP: classes.txt (sorted), task_<id>.txt with
   `<idx> 0.5 0.5 1.0 1.0` placeholder lines, README.txt, manifest.
   CSV formula-injection guarded; UTF-8 BOM.
5. **Notifications:** notify(user,kind,payload,subject,body) never
   raises; in-app always, email only if SMTP_HOST+subject; bell badge
   via context processor (failure→0); feed = 50 newest,
   mark-read-on-view.
6. **Preference (RLHF/DPO) task type:** task_type "preference";
   input_data = JSON {kind, prompt, response_a, response_b}; optional
   gold "a"/"b". Workspace: prompt + side-by-side response cards;
   position flip = deterministic hash of (task_id, annotator_id)
   computed on GET and POST (client never sends order). Form: chosen
   RadioField (a/b as DISPLAYED) + required justification (min
   PREFERENCE_MIN_JUSTIFICATION=40). Stored output: canonical chosen,
   seen_order, justification — map back through the flip server-side.
   Review renders canonical A/B columns + justification; quality gold
   accuracy compares chosen vs gold; BOTH import paths accept JSON
   preference lines with gold normalized at preview AND confirm.

7. **Consensus (RLHF Phase 2):** projects.required_annotations 1-3
   (CHECK); depth >1 → every imported row becomes a ConsensusGroup
   with N identical sibling tasks (tasks.consensus_group_id FK).
   Claims AND admin assigns refuse a second sibling of a held group.
   Evaluation on submit + verdicts (consensus.py): all siblings
   answered + same verdict key → agreed; differ / free-text →
   conflict; latest submission rejected → pending (verdict withdrawn).
   Arbitration page records a BINDING ruling (choice, note, auditor,
   time) → resolved + FROZEN (never re-evaluated), audited
   consensus.resolve. Exports carry consensus status + decision.

8. **Trusted rendering (RLHF Phase 3):** task content is untrusted;
   render it SERVER-SIDE only: markdown (fenced_code, tables, codehilite
   via Pygments CSS classes, nl2br) → nh3 strict allowlist sanitizer
   (no script/img/iframe/event-handlers; http/https/mailto links only;
   span/pre keep class). conversation JSON
   {kind:"conversation",turns:[{role:user|assistant|system,text}]}
   renders as chat bubbles. Oversize/invalid → escaped plain text;
   never raise. Templates use |safe ONLY on these sanitized views and
   a system-check enforces the allowlist. Self-hosted pygments.css —
   no CDN, no client-side rendering. Pin markdown, nh3, pygments.

9. **SFT gold-rewrite builder (RLHF Phase 4):** sft.py (pure,
   never raises) harvests finalized work into training pairs —
   completion priority: resolved consensus ruling > approved
   submission > gold expected_output; prompt from preference doc /
   conversation last user turn / text-raw. Table sft_examples keeps
   an immutable source_completion anchor; a written edit note is
   REQUIRED whenever the editable completion deviates. Lifecycle
   draft → ready → exported (exported terminal + frozen). Export is
   POST-only chat-messages JSONL capped by SFT_EXPORT_MAX_ROWS,
   frozen in the same transaction as its audit event. Decorator
   order is load-bearing: @admin_bp.route ABOVE @admin_required
   (inverted = the role wall never registers).

10. **Rubric sliders (RLHF Phase 5):** preference projects may
    define rubric dimensions (newline list on the project form,
    validated by pure rubric.py). Annotators rate BOTH responses on
    each dimension with NATIVE <input type="range"> sliders — no
    client JS. Fields are named by INDEX (rubric_0_a…) and map to
    the canonical side server-side (same mapping as the choice
    radios); scores live in the output document
    {"rubric":{dim:{"a","b"}}}, are re-validated for display, and
    never change consensus verdict keys.

11. **Staff roles (Phase 6):** four roles — admin (everything),
    qa (review queue + quality), auditor (arbitration + audit
    trail), annotator (workspace). Shared decorators @qa_required /
    @auditor_required live in routes/__init__; ck_users_role carries
    the vocabulary; sign-in routes each role to its queue; nav and
    role badge are role-scoped. The system-check matrix accepts the
    named decorators — a route registered without one fails the
    battery.

12. **Paystack Transfers (Tier 2):** optional via PAYSTACK_SECRET_KEY.
    paystack.py never raises; recipient cached on the user; kobo at
    one rounded Decimal boundary; reference stored on the payment;
    webhook POST /webhooks/paystack authenticates by HMAC-SHA512 of
    the raw body (the CSRF matrix's only documented exemption) and
    settles idempotently. Account numbers never reach logs/audit.

13. **Client portal (Phase 7):** /client/<token> — the token is the
    credential: 32-byte urlsafe, store ONLY its sha256, redact it
    from access logs, never reflect it into page text (prebuild the
    export URLs server-side), noindex the pages, 404 dead links.
    Deliveries carry approved rows WITHOUT annotator identity, gold
    markers or reviewer notes. Raw link is shown to the admin exactly
    once (temp-password contract) and revocation is instant+audited.

14. **Media workspaces (Phase 8):** image_labeling + audio_
    transcription projects. Admins upload to a per-project library
    (`/admin/projects/<id>/media`): MEDIA_MAX_BYTES cap, magic-byte
    sniffing (`_sniff_media` — SVG never, RIFF disambiguates
    WebP/WAVE, ftyp→audio/mp4), sha256 per-project dedupe (unique
    index), audited. Bytes live in Postgres LargeBinary (documented
    deviation: $0, inside pg_dump, survives redeploys; R2 later is
    additive). Tasks reference media via input doc
    `{"kind", "media_id", "prompt"?, "labels"?}`; the annotator
    route `/task/<tid>/media/<aid>` is task-bound (doc must
    reference the asset, same project, caller's task — else 404);
    staff serve `/admin/media/<id>` for admin/qa/auditor. Outputs:
    `{kind: image_labeling, label, notes?}` /
    `{kind: audio_transcription, transcript}`. QA review +
    arbitration render the same media. Media labels travel to the
    form builder as `label_choices`, NEVER through the rubric_dims
    slot (invisible sliders brick the POST); on WTForms ≥3.2 read
    uploads from `request.files` (FileField.data is a str).

15. **Qualification quizzes (Phase 9):** one quiz per project
    (quizzes.py owns parse/grade; paste format `Q:` / `- ` / `* `).
    Active quiz gates claim + assignment + task page on
    passed/exempt attempts; dashboard card + `/annotator/quiz/<id>`;
    admin console activates/retires/grants audited exemptions. The
    answer key never renders; display order is blake2b per
    (quiz, annotator); attempt cap from failed tries; pass terminal.

16. **Machine API (Phase 10):** /api/v1 under api_bp, Bearer-key
    auth only (no cookies/CSRF/session path); project-scoped keys
    (foreign projects uniform 404); key_hash stored only, raw key
    shown once at issue, revoke instant; per-key minute rate limit
    via kvstore (optional Redis with in-process fallback). Endpoints:
    /ping open, /me, /projects/<id> progress, /projects/<id>/results
    (paginated, client_safe_record contract — no identity/gold),
    POST /projects/<id>/tasks (batch validation, caps, paused 403,
    audit counts only).
17. **Background jobs (Phase 10):** background_jobs table
    (queued/running/done/failed ck, composite status+run_after
    index); conditional UPDATE claiming so in-process runner or
    dedicated worker.py share safely; exponential backoff retries
    (max 5 attempts default, capped 20); boot healing requeues
    'running' rows from killed processes; today runs 'email.deliver'
    (SMTP off the request path) and 'paystack.reconcile' (pending
    payout sweep, idempotent with the webhook pending-guard);
    JOBS_ENABLED / BRYDL_DISABLE_JOBS kill switches; Redis optional.
    No secrets in payloads.

18. **Password reset:** urlsafe 32B token, store sha256 only, TTL 3600,
   single-use, 3/15-min rate limit, enumeration-safe, bump
   auth_version on success (kills sessions).
19. **Login lockout:** 5 failures → exponential 120 s→900 s lockout,
   generic errors.
20. **Config:** every knob env-driven — PAGINATION_PER_PAGE(20),
   IMPORT_MAX_ROWS(2000), IMPORT_MAX_BYTES(2MB),
   MAX_QA_NOTES_CHARS(2000), UPLOAD_BATCH_LIMIT(500),
   EXPORT_YIELD_PER(500), LOGIN_*(5/120/900), FORGOT_*(3/900),
   RESET_TOKEN_MAX_AGE_SECONDS(3600), BRYDL_TERMS_VERSION("1.0"),
   BRYDL_AUTO_SCHEMA(1; 0=prod: migrations authoritative + boot drift
   guard raising RuntimeError with remediation), CLAIM_DAILY_CAP(20),
   TASK_MAX_REJECTIONS(3), SITE_URL, BANK_ENC_KEY,
   USD_TO_NGN_RATE(1600 Decimal), SMTP_HOST/PORT/USERNAME/PASSWORD/
   MAIL_FROM/STARTTLS. SECRET_KEY: env → generated instance file
   fallback. SQLite engine: creator = per-path factory
   `_sqlite_connect_with_retry(path)` (4 attempts, 0.01·(n+1) backoff,
   timeout=15 INSIDE the creator — connect_args ignored with a
   creator); PG: connect_timeout=10; both pool_pre_ping.
21. **Security:** role decorator on every protected route; annotator
   blueprint before_request terms gate (302 except terms pages);
   fail-closed user_loader; session strong; cookies HttpOnly+
   SameSite=Lax+Secure(prod); headers nosniff/DENY/same-origin/CSP/
   no-store; audit every privileged mutation (counts only); server-
   side validation everywhere; try/except around DB ops with honest
   flashes; UTC everywhere; os.path.join everywhere; zero hardcoded
   values.

## FRONT END

base.html shell (role-aware navbar + bell badge, flashes, self-hosted
BS5/Icons, theme toggle), admin templates (dashboard, projects,
project_form, project_export, tasks, import preview, review,
review_detail, quality, payments, users, audit), annotator templates
(dashboard w/ Claim button + cap badge + deadlines, task workspace,
history, notifications, profile, terms), auth pages (login, forgot,
reset), landing, offline, errors. Clean Bootstrap 5, no custom JS
framework.

## DEPLOY

render.yaml (Oregon — NEVER fra), Procfile gunicorn, runtime.txt 3.13,
.env example with every var documented. Boot: BRYDL_AUTO_SCHEMA=0 →
migrations authoritative; linear Alembic chain covering every schema
change above (batch_alter_table on SQLite).

## TESTS (with every feature, not after)

Granular expect()-style suites, ≥ the following gates, all green with
flake8: (a) migrations: create_all DDL == upgraded DDL, db check
clean, drift guard loud; (b) stress: 60 concurrent annotators ×2
waves exactly-once, p95 ≤ 409 ms; (c) claim: 9-way race = 9 distinct
winners + cap + CSRF + paused refusal; (d) notifications lifecycle +
badge; (e) revision cap escalation; (f) bank encryption round-trip +
masked UI + CSV decrypt + legacy tolerance; (g) exports: COCO schema
refs valid, YOLO zip structure, gold never in COCO/YOLO, audit rows;
(h) reset/lockout/hardening; (i) import preview/confirm limits.
**Runner rule: wipe instance/ ONLY from the shell BEFORE the suite
process — never rmtree inside a suite (it strands new pooled SQLite
connections → fail-closed loader → phantom auth failures).**

## FINAL GATE

Full battery green + flake8 clean + docs (README, ROADMAP,
SECURITY_AUDIT ledger, DEPLOY_GUIDE, POST_DEPLOY_CHECKLIST,
.env example) + a blueprint/ folder (this spec, kept updated with
every change per its update protocol).
