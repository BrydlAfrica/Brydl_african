# Brydl Dashboard — Security & Integrity Audit

**Last updated:** 2026-09-10 (after auth feature build) ·
**Scope so far:** V1 scaffolding + auth feature (login/logout, RBAC,
base/login templates) ·
**Method:** manual OWASP-style review of every layer + live exploit probes
against the running app + reading installed Flask-Login / flask-wtf source.

Every "fixed" item below is covered by an automated verification that was
run and passed. This file is updated whenever hardening work happens, so
"not mentioned here" means "not yet audited."

---

## 1. Fixed — scaffolding hardening (build 1)

| # | Severity | Finding | Fix | Where |
|---|----------|---------|-----|-------|
| F1 | **HIGH** | SQLite silently ignores foreign keys by default — probes stored a task with `assigned_to=99999` (a user that does not exist). Every `ForeignKey()` was decorative. | Per-connection `PRAGMA foreign_keys=ON` listener on the SQLAlchemy Engine (no-op for PostgreSQL drivers, which enforce FKs natively). Deletes of referenced users/projects are now blocked too — payroll history can't be orphaned. | `extensions.py` |
| F2 | **HIGH** | Flask-Login only checks `is_active` inside `login_user()` — **never** when reloading a session or remember-me cookie (confirmed in installed source). A deactivated annotator would keep full access for up to 7 days. | `user_loader` rejects inactive users and purges the stale session identity (`_user_id`, `_fresh`, `_remember`). Verified: deactivate → next request is 302 to login with session purged; re-activation does **not** resurrect the old session; mid-session deactivation also bounces immediately. | `app.py` |
| F3 | **HIGH** | No browser security headers: clickjacking possible, MIME-sniffing possible, no CSP, no referrer control. | `after_request` hook sets `Content-Security-Policy` (Bootstrap CDN allow-listed; scripts never inline; `form-action 'self'` = browser-level CSRF layer; `frame-ancestors 'none'`), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Permissions-Policy`. Any new CDN origin must be added to `CONTENT_SECURITY_POLICY` in `app.py`. | `app.py` |
| F4 | **MEDIUM** | Workflow contradictions were storable: probes saved a `pending` task **with** an assignee, and an `assigned` task with none. | CheckConstraints: pending ⇔ unowned pairing (`tasks`), approved/rejected submissions must carry `reviewed_at` + `qa_score`, paid payments must have `paid_at` and `payment_method`. All rejection paths verified. | `models.py` |
| F5 | **MEDIUM** | A short `SECRET_KEY` env value (e.g. `"abc"`) would be silently accepted — a brute-forceable key lets attackers forge session cookies. | `_load_secret_key()` raises `ValueError` at boot for env keys < 32 chars, with the generation command in the message. | `config.py` |
| F6 | **MEDIUM** | Render terminates TLS at its edge; the app would believe every request is plain http, and session/remember cookies had no `Secure` flag in production. | `ProxyFix` applied only when `RENDER` env var present; `SESSION_COOKIE_SECURE` / `REMEMBER_COOKIE_SECURE` auto-enable on Render; HSTS header added there. Local http dev unaffected. | `app.py`, `config.py` |
| F7 | **MEDIUM** | SQLite default rollback-journal mode blocks all readers during a write — with 5–50 concurrent annotators this surfaces as stalls and "database is locked". | `PRAGMA journal_mode=WAL` per connection (verified: was `delete`, now `wal`). | `extensions.py` |
| F8 | **LOW** | SECRET_KEY fallback chain operated silently. | Warning is logged whenever the fallback key is generated (visible in Render logs). | `config.py` |
| F9 | **LOW** | No `.gitignore`: `instance/` (contains the DB **with password hashes** and the secret key) could be committed. | `.gitignore` added. | `.gitignore` |
| F10 | **LOW** | Transitive dependencies (SQLAlchemy, WTForms) unpinned. | Pinned explicitly in `requirements.txt`. | `requirements.txt` |
| F11 | **LOW** | Connections dropped by hosted-DB idle timeouts would surface as random 500s. | `pool_pre_ping=True` in engine options. | `config.py` |
| F12 | INFO | No switch path to PostgreSQL existed. | `DATABASE_URL` env var overrides the SQLite URI, including rewriting Render's legacy `postgres://` scheme. | `config.py` |

## 2. Fixed — auth feature (build 2)

| # | Severity | Finding | Fix | Where |
|---|----------|---------|-----|-------|
| A1 | **HIGH** | Login had no rate limiting — unlimited online password guessing. | In-memory per-email+IP lockout: 5 failures → escalating lockout (2→4→…→15 min cap), HTTP 429. Keyed to email+IP so a third party cannot lock a victim out from an unfamiliar address. Single-worker-safe on Render free tier; move to DB/Redis when workers scale out. Verified: 5th failure locks even the correct password out. | `routes/auth.py` |
| A2 | **HIGH** | `?next=` redirects after login are a classic open-redirect hole. Flask 3.1 / Werkzeug 3.1 no longer ship `url_has_allowed_host_and_scheme`, so nothing validated `next`. | `_is_safe_redirect()`: only root-relative paths with no scheme/netloc pass. Verified: `https://evil.com`, `//evil.com`, `javascript:alert(1)` all ignored; internal `/path` honoured. | `routes/auth.py` |
| A3 | **MEDIUM** | User enumeration via response differences: different error for "unknown email" vs "wrong password", and scrypt timing reveals whether an email exists. | Single generic "Invalid email or password." message; a dummy scrypt hash is compared when the email is unknown so timing is equalised. Verified: both cases return the identical message. | `routes/auth.py` |
| A4 | **MEDIUM** | Session fixation: logging in did not rotate session content. | `session.clear()` before `login_user()` — no pre-login session data carries into the authenticated session. | `routes/auth.py` |
| A5 | **MEDIUM** | Logout via GET would let link previews / cross-site tags sign users out; naive logout also skips CSRF. | `/logout` is POST-only + `validate_csrf()` (bad token → warning flash, still signed in). Verified: GET → 405; forged CSRF refused. | `routes/auth.py`, `templates/base.html` |
| A6 | **MEDIUM** | Email case sensitivity: SQLite uniqueness is case-sensitive, and `Ada@Brydl.NG` would fail login against stored `ada@brydl.ng` (and could later create a duplicate account). | Form-level filter strips+lowercases **before validation runs**, view normalizes again as defence in depth. Verified: `"  Admin@Brydl.NG "` logs in. | `routes/auth.py` |
| A7 | **LOW** | `csrf_token()` template global is only registered by `CSRFProtect.init_app`; with per-form validation (our setup) it doesn't exist — navbar logout form would crash. | `app.jinja_env.globals["csrf_token"] = generate_csrf` registered explicitly in the factory. | `app.py` |
| A8 | **LOW** | Expired CSRF token on the login form produced a raw WTForms error string. | Friendly warning alert on `login.html` ("this form expired — please try again"). | `templates/login.html` |
| A9 | INFO | CDN supply-chain risk: templates pull Bootstrap from jsdelivr with no integrity check. | All three CDN assets pinned to exact versions with `integrity="sha384-…"` (SRI) + `crossorigin="anonymous"`. Verified hashes were computed from the exact files served. **When bumping Bootstrap, regenerate the SRI hashes.** | `templates/base.html` |

## 3. Deliberate decisions (assessed, no change)

- **Money as Float USD** (master spec): safe at this scale; helper math
  rounds to 2 decimals. Revisit `Numeric` during the PostgreSQL migration
  if aggregation volumes grow.
- **CSRF token lifetime 12h** (vs 1h default): deliberate
  intermittent-connectivity accommodation; mitigated by `SameSite=Lax`
  cookies and CSP `form-action 'self'`.
- **Session protection "basic"** (Flask-Login default): "strong" would
  constantly log out mobile annotators whose IPs change — wrong trade-off
  here.
- **`create_all()` fails fast at boot**: correct while Render free tier
  runs a single gunicorn worker.
- **FK `RESTRICT` default** (no cascades): users are deactivated, never
  deleted.

## 4. Scheduled — belongs to a future feature

**Task-completion feature:**
- Never render `Task.expected_output` to annotator routes/templates — it is
  the QA answer key (flagged on the column).
- Decide revision flow: resubmit = update row vs new row (audit trail).
  Only after that can a `UNIQUE(task_id, annotator_id)` be considered.
- Advance task status and insert the submission inside one transaction.

**User management (admin feature):**
- Minimum password length policy + forced change on first login.
- `flask seed-admin` CLI so the first admin never goes through a public
  signup path (until then, create admins via a Python shell).

**Deployment phase:**
- Point `DATABASE_URL` at Render PostgreSQL + add `psycopg2-binary` +
  Flask-Migrate (SQLite on ephemeral disk loses data on redeploys).
- Keep gunicorn at 1 worker while SQLite remains the store.
- Set a 32+ char `SECRET_KEY` in the Render dashboard before first deploy.

## 5. Development notes (lessons captured)

- **Never wrap test-client requests in an outer `app.app_context()`.**
  Flask reuses an already-active app context (flask/ctx.py:380), `g` lives
  on it, and Flask-Login caches the loaded user on `g._login_user` — so
  requests from *different* test clients share one identity and tests
  mysteriously cross-contaminate. Structure: do data setup inside `with
  app.app_context():`, run all client requests outside it. This cost a
  long debugging session to find; it is a harness bug, not an app bug
  (real HTTP requests always get their own app context).
- **ORM default values fire at flush time**, not at construction —
  `obj.created_at` read before `commit()`/`flush()` is `None`.
- **Attribute access on ORM objects outside their session raises
  `DetachedInstanceError`** — capture ids while the context is open.

## 6. Verification log

- 2026-09-10 (build 1): pre-fix probes reproduced F1/F3/F4; post-fix, 12
  automated checks passed (FK rejections, invariants, WAL, headers,
  env-var matrix, deactivation session-kill).
- 2026-09-10 (build 2): 15 automated auth tests passed — generic errors,
  no-enumeration, lockout 429, role-based redirects, cross-role 403 both
  ways, anonymous bounce with `next`, GET-logout 405, evil-`next` blocked
  (3 vectors), internal-`next` honoured, logout-CSRF refusal,
  remember-me issuance, mid-session deactivation bounce, deactivated
  login rejected, template CSP hygiene. `flake8`: 0 violations.

## Audit trail & logging (frontend-polish build)

- `audit.py`: structured stdout logging (`%(asctime)s LEVEL [request_id] ...`)
  + `log_event()` writing an immutable `audit_logs` row. `REQ` access lines,
  `AUDIT` action lines and tracebacks share a per-request 12-hex
  correlation id, also echoed as `X-Request-ID`.
- Audited actions: login success/failed/locked-out (with failure reason),
  logout, logout-CSRF-reject, project create/update/pause/resume, task
  upload/assign/unassign, submission create, review verdict/bulk,
  payment generate/mark-paid. Actor, entity, IP, user-agent, JSON detail.
- `/admin/audit` (admin-only): filterable trail browser (actor, action
  substring; wildcards stripped; tampered params render empty, not 500).
- In-transaction audits (`commit=False`) commit/roll back atomically with
  the action they describe; standalone audits use `commit=True`.
  `log_event()` never raises — the stdout line is written first.

## Backup system & deployment readiness (deploy build)

- `backup.py` — `flask --app app backup create|list|verify|restore`:
  - `create`: SQLite online backup API (consistent under concurrent
    writes; WAL content included). Writes a sidecar manifest (sha256,
    size, integrity_check result, per-table row counts) and prunes to
    `BACKUP_KEEP` (default 14, env-overridable). Snapshots go to
    `BACKUP_DIR` (default `backups/`, gitignored).
  - `restore`: checksum-verifies the snapshot against its manifest,
    stages a copy, removes stale `-wal`/`-shm` sidecars, then
    `os.replace()`s onto a NEW inode (atomic). In-place overwrite is
    unsafe with WAL: un-checkpointed `-wal` transactions replay over
    the restored file, and a later last-connection-close checkpoint
    scrapes old pages back (both observed in testing before the
    new-inode design). Old connections keep a coherent unlinked inode;
    the command tells the operator to restart the web service. The
    post-restore audit row is written only after the engine is
    disposed, through a fresh connection to the restored file.
  - Dry-run by default (`--yes` required); bare-filename validation
    blocks path traversal; non-SQLite engines are refused with
    pg_dump/managed-backup guidance. `system.backup` / `system.restore`
    are written to the audit trail.
- `seed.py` — dev-only demo data (1 admin, 5 annotators, 1 project,
  10 tasks, 4 pre-assigned). Refuses to run when `RENDER` or
  `FLASK_ENV=production` is set; idempotent (get-or-create); writes a
  `system.seed` audit row; seeded passwords are weak placeholders.
- Render assets: `Procfile` (`gunicorn "app:create_app()"`),
  `runtime.txt` (python-3.11.6), `render.yaml` (free web service,
  generated SECRET_KEY, FLASK_ENV=production, health check on /login,
  commented PostgreSQL upgrade path), `.gitignore` extended with
  `backups/`, `requirements.txt` carries a commented
  `psycopg2-binary` for the DB migration.

## Scalability hardening (scalability build)

- **Money = Decimal end to end**: `Project.pay_per_task` / `Payment.amount`
  are `db.Numeric(10, 2)`; new `models.money()` quantizes every Python
  aggregation boundary to 0.01 (half-up; float conversion via `repr()`).
  WTForms `DecimalField` on the project form; NGN conversion returns
  Decimal. Dead `users.pay_rate` Float column removed.
- **Indexes**: composite indexes for the hot paths —
  `ix_tasks_assignee_status`, `ix_tasks_project_status`,
  `ix_submissions_annotator_status`, `ix_submissions_status_payment`,
  `ix_submissions_annotator_submitted`, `ix_payments_annotator_status`,
  `ix_payments_status_paid_at`, `ix_users_role_active`.
- **Pagination**: every list view paginates at `config.PAGINATION_PER_PAGE`
  (20) — the projects list and the audit trail (was 50) were added.
- **N+1**: measured with a `before_cursor_execute` listener — the tasks
  list stays at 6 queries when rows grow 5x (batched IN lookups);
  review-detail now eager-loads task→project+annotator in one SELECT.
- **Concurrency**: gunicorn `--threads 4` (Procfile + render.yaml),
  pool_size 10 / max_overflow 20, WAL mode, 15 s busy timeout. Verified
  with 30 parallel clients.
- **Config-driven tunables**: pagination size, upload batch limit, QA
  notes cap, login throttle ladder, FX rate — all in `config.py`
  (env-overridable, mirrored module-level + Config class).
- NOTE: schema changed (Numeric columns, removed column, new indexes) —
  delete the local `instance/brydl.db` once; on PostgreSQL use
  Flask-Migrate.

## Full-system check (system-check build)

- `tests/test_system_check.py` — 31 automated sweeps: route-table sanity
  (documented views, no duplicate rules), a CSRF/role-guard matrix over
  every route (read from each view's original source through the
  decorator chain), a three-role crawl of every GET route (no 5xx for
  anonymous/annotator/admin; role walls verified), template compilation,
  url_for/endpoint and literal-href resolution against the URL map,
  config-key coverage, full CHECK-constraint + index inventory,
  integrity/foreign-key checks, danger-pattern scan (TODO/print/pdb/
  `|safe`/inline handlers/leaked seed passwords/absolute paths), branded
  error pages for 403/404/405/500 without exception leakage, static
  assets, and requirements-pin vs installed-version verification.
- Added the missing branded 405 page (`errors/405.html` + handler) —
  the last bare framework error page.

## User management & seed-admin (team build)

- **Team page** (`/admin/users`, admin-only): create accounts
  (server-generated one-off password shown ONCE — never logged/audited),
  deactivate/reactivate (live sessions revoked via the user loader;
  history/payouts kept; accounts never deleted), reset password
  (one-off again). Guards: no self-deactivation, last-active-admin
  cannot be dropped. Audited: `user.create`, `user.deactivate`,
  `user.reactivate`, `user.password_reset`. Last-login column derived
  from the audit trail (no schema change). List paginated 20/page.
- **`flask --app app seed-admin`**: first-admin bootstrap; refuses when
  any active admin exists (further admins via Team page), validates
  email + 8–128 char password (prompt, `--password`, or
  `SEED_ADMIN_PASSWORD`), normalises email lowercase, audited as
  `system.seed_admin`.

## Brand & landing build

- **Landing page** (`/`, `routes/public.py`): the only public route.
  Authenticated visitors are redirected to their role dashboard; no
  app data is rendered — only static marketing copy. CSP, headers and
  CSRF posture unchanged (no new origins; images self-hosted under
  `static/brand/`, so `img-src 'self' data:` already covers them).
- **Signature boot overlay**: pure-CSS timeline that ends hidden even
  if JavaScript never runs (`animation-fill-mode`), honours
  `prefers-reduced-motion` (display:none), plays once per session
  (sessionStorage), hard-capped by JS at 3.4 s. Landing + login only —
  app pages keep the slim progress bar.
- Brand palette moved from green to the logo navy (#173363) via CSS
  custom properties; no markup/behaviour changes. New suite:
  `tests/test_brand.py` (18 checks).

## Annotator experience build (terms, profile, theme)

- **Terms & Conditions gate**: annotator-only before_request on the
  blueprint; anything but the terms/profile endpoints redirects to
  `/annotator/terms` until `user.terms_version` equals
  `config.TERMS_VERSION`. Acceptance is POST + CSRF only, stores the
  version + UTC timestamp, and is audited (`user.terms_accept`).
  Bumping the config version (env `BRYDL_TERMS_VERSION`) re-prompts
  the whole team. Consent semantics: model default = current version
  (seeded/scripted accounts count as accepted); the Team page creates
  accounts with `terms_version=""` (must accept). "" is the sentinel
  because a Python-side column default always replaces NULL.
- **Profile self-service** (`/annotator/profile`, annotator-only):
  rename + password change (current password verified first; never
  logged) + theme picker. Audited: `user.profile_update`,
  `user.password_change`, `user.theme`. Two forms share the page with
  distinct submit-field names; CSRF validated before any branch.
- **Theme**: `/account/theme` (POST + CSRF, any signed-in user);
  whitelisted to light/dark; rendered server-side via
  `data-bs-theme`; dark values re-map the custom CSS tokens only.
- **Continue-working**: POST `/annotator/continue` opens the oldest
  open task (same ordering/query as the dashboard queue).
- **Known limitation**: password change does not revoke other live
  sessions (Flask-Login has no per-session token store); admins can
  still full-reset via the Team page.

## Export kit build

- **Dataset export** (`GET/POST /admin/projects/<id>/export`,
  admin-only): JSONL/CSV with format + scope (approved-only default,
  reviewed, all) options. **Gold answers excluded by default** — the
  training file cannot leak the answer key by accident; inclusion is
  an explicit opt-in checkbox. Annotators are pseudonymous (numeric
  ids, never names/emails). Rows stream with `yield_per`
  (`config.EXPORT_YIELD_PER`) — flat memory regardless of project
  size; submissions join tasks once (no N+1). Audited as
  `project.export` with format/scope/row-count — the audit trail
  never carries exported content.
- **Bank batch CSV** (`POST /admin/payments/bank-batch`, admin-only,
  CSRF): every pending payment as a bank-ready row — reference
  (`BRYDL-PAY-<id>`), recipient name/email, exact Decimal NGN amount
  at the configured rate (stamped per row), period + narration.
  Audited as `payment.export_batch` (count + totals only). Executing
  the transfer remains a human step; marking paid is unchanged.

## PostgreSQL + Flask-Migrate cutover

- **Two schema paths, one source of truth.** Local dev/tests build the
  schema with `db.create_all()` (gated by `BRYDL_AUTO_SCHEMA`, default
  on); production (render.yaml sets `BRYDL_AUTO_SCHEMA=0`) uses
  EXCLUSIVELY `flask db upgrade` from the `migrations/` package, run in
  the Blueprint build command before the service starts.
  `tests/test_migrations.py` proves the two paths produce identical
  schemas (normalized DDL parity) and that `flask db check` reports no
  model/migration drift.
- **Engine options are backend-aware** (`config._engine_options`):
  SQLite keeps its busy timeout; PostgreSQL gets
  `connect_timeout=10` + the same pool sizing (pre_ping, 10/20).
- **Render legacy URI**: `postgres://` is rewritten to
  `postgresql://` in config (unit-tested).
- **Blueprint**: `databases:` block active (free plan, expires ~30
  days — README Step 0/4 carry the upgrade path);
  `DATABASE_URL` injected via `fromDatabase: connectionString`.
- **Known limitation (written explanation)**: no SQLite→PostgreSQL
  data-migration path is provided — this project is pre-production by
  design ("fix every loop hole before we move next"), so the cutover
  assumes a fresh database + `seed-admin`. If local data ever needs
  moving, `flask backup restore` into SQLite plus a one-off ETL script
  is the honest tool for the job, not a half-automatic cross-dialect
  copy.

## Offline PWA build

- **Service worker** (served at root scope `/sw.js`, no-cache so
  updates roll out next visit; `CACHE_VERSION` busts caches):
  - POST requests are NEVER intercepted — submissions, logouts and
    admin mutations always reach the server (CSRF integrity).
  - Navigations are network-first; the HTML of authenticated pages is
    never cached (stale/misleading content + shared-device leakage).
    A failed navigation gets the branded `/offline` page.
  - The SRI-pinned jsDelivr CDN assets are cache-first (immutable
    content — safe to cache forever) so pages stay styled offline.
  - Same-origin `/static/`: stale-while-revalidate. Registration is
    skipped on localhost/127.0.0.1 so dev edits never hide behind a
    cache; kill switch = serve 404 for `/sw.js` or drop registration.
- **Draft autosave**: every annotator answer form carries
  `data-draft-key="task-<id>"`; main.js saves on input (400ms) to
  localStorage and restores into EMPTY fields only (never clobbers
  server-rendered values on failed validation). Drafts stay on the
  device; logout wipes every `brydl-draft-*` key (shared-device
  hygiene). The dashboard renders `data-draft-purge` markers for
  submitted/approved tasks so stale drafts die server-driven;
  rejected tasks keep their draft (revision in progress).
- **PWA manifest** (`/manifest.webmanifest`): standalone display,
  brand navy theme, 192+512 icons — annotators can "Add to Home
  screen" and launch Brydl like an app on flaky connections.

## Self-service password reset (email token)

- **No account enumeration**: the response is byte-identical whether
  the email exists, is inactive, or is unknown; inactive accounts are
  issued nothing (reactivation is an admin action).
- **Token discipline**: 32-byte `secrets.token_urlsafe` token in the
  link; ONLY its SHA-256 hash is stored (`users.reset_token_hash`)
  with a UTC expiry (`reset_token_expires`, default 1h via
  `RESET_TOKEN_MAX_AGE_SECONDS`). Consumption clears both columns —
  every link is single-use; replay/expired are refused identically.
- **Anti email-bombing**: per-email in-memory throttle
  (`FORGOT_MAX_PER_WINDOW` per `FORGOT_WINDOW_SECONDS`, default 3 per
  15 min); over-throttle requests are audited
  (`auth.password_reset.throttled`) and send nothing.
- **Delivery**: `mailer.py` (stdlib smtplib, STARTTLS/implicit TLS)
  via `SMTP_*`/`MAIL_FROM` env knobs. Without `SMTP_HOST` (local dev)
  the link is written to the app log and the flow completes — dev
  fallback only. Delivery outcome audited (`delivery: smtp|dev_log`,
  `sent`).
- **Audit family**: `auth.password_reset.requested`, `.throttled`,
  `.invalid_link`, `auth.password_reset` (the change itself,
  actor-attributed).
- **Known limitation**: resetting a password does not invalidate other
  already-signed-in sessions (Flask-Login has no per-session token
  store). Admins can hard-revoke via Team → deactivate. All
  comparisons use the naive-UTC `models.utc_now()` — mixing with
  timezone-aware datetimes raises TypeError (caught by this suite).

## Build 21 — hardening + payout accounts

- **Schema drift guard** (create_app): if a database predates the
  models, boot FAILS LOUDLY naming the missing columns and the fix —
  instead of the old behaviour (healthy boot, then 500 with
  `no such column` on every user query). Skipped during
  `flask db …` commands (upgrading IS the remediation; autogenerate
  must run against stale databases). Honest caveat in the message:
  pre-migration databases cannot be upgraded in place (revision 1
  recreates tables) — local dev deletes `instance/` and reseeds.
- **Terms gate tightened**: the ONLY endpoints reachable before
  accepting the current terms are the terms page and its acceptance
  POST (profile was previously exempt — that loophole is closed, and
  login now lands unaccepted annotators on the terms page first).
- **Payout accounts**: bank_name / bank_account_number (text —
  leading zeros survive) / bank_account_name, annotator-entered,
  CSRF-only endpoint, NUBAN = exactly 10 digits, audited
  (`user.bank_details_update` / `_remove`) with the account number
  NEVER in audit details. Admin visibility: masked (last 4) on the
  Team page, full only in the bank-batch CSV. Removal clears all
  three columns. Migration `041200d1ec79` adds the columns;
  parity + `db check` green; downgrade verified.

## Scalability re-audit (post-Build 21)

All seven BUILD-12 requirements re-verified against the current tree;
one gap found and fixed:

- **Gap**: `users.reset_token_hash` became a filtered column in the
  password-reset build with no index. Fixed via migration
  `f7353e97b8cc` (`ix_users_reset_token_hash`, non-unique); chain now
  4 revisions, parity + `db check` green.
- **Index inventory (6 composite + singles)**: users(role,is_active),
  users.reset_token_hash, tasks(assigned_to,status),
  tasks(project_id,status), submissions(status,payment_id),
  payments(annotator_id,status), payments(status,paid_at) plus
  single-column indexes (created_at, FK/status columns, unique email).
  Every WHERE-filtered column in routes is covered (script-verified).
- **Pagination**: 9 paginate() calls across admin (7) + annotator (2),
  ALL deriving from config.PAGINATION_PER_PAGE via named aliases
  (TASKS/REVIEWS/AUDIT/HISTORY_PER_PAGE). Downloads stream with
  yield_per instead (memory-flat by design, not a list view).
- **N+1**: 4 eager-load directives, exports join (Submission, Task)
  as a single streamed SELECT, zero per-row session.get in loops;
  scalability suite asserts flat query counts at 5x data volume.
- **Concurrency**: 30-parallel-client barrier in the suite; live
  report 400 requests / 16 threads at ~200-240 req/s, 0 errors;
  12-racer exactly-once submissions. Gunicorn 4 threads/worker.

## "Make all fix" — credential-change session revocation (auth_version)

**Date:** 2026-09-13 · **Scope:** cross-device session invalidation on any
password change · **Result:** all fixes verified, 16/16 suites green.

**Gap closed.** Until now, changing or resetting a password left every
already-issued session cookie (including remember-me) valid until natural
expiry — a stolen session survived a credential change.

**Fix — auth_version counter:**
- `users.auth_version` (Integer, NOT NULL, server_default 0) — migration
  `736be0df0ee9` on top of `f7353e97b8cc`; schema parity + `flask db check` green.
- `User.get_id()` now returns `"id.auth_version"`, so the version is baked
  into every Flask-Login session id and remember-me cookie.
- The user loader partitions the stored identifier: legacy bare-id cookies
  are treated as version 0 (graceful upgrade, no forced logout of current
  users); a version mismatch — or an inactive account — purges
  `_user_id` / `_fresh` / `_remember` and resolves to anonymous.
- Counter bumped at all three credential-change sites: annotator
  self-service change (which also signs that session out and redirects to
  login), email reset confirmation, admin password reset.

**Verified by new hard assertions (not tautologies):**
- a second live session for the same account receives a redirect to login
  immediately after a self-service password change;
- the session that performed the change is signed out;
- admin reset and email reset each revoke the account's other live sessions;
- re-login with the new credential restores access.

**Hygiene closed in the same round:** the two self-review tautologies were
replaced with real checks (login boot quick-variant class present; keyboard
picker present in main.js), and the profile help-text now states the new
behaviour ("signed out ... on every device").

## CSV task import — file-upload hardening (feature round)

**Date:** 2026-09-13 · **Scope:** admin bulk task import from a device
file (Tasks tab → *Import from file*) · **Result:** 18/18 suites green
including the new tests/test_import.py (35 checks).

**Attack surface considered and closed:**
- **Role + CSRF**: admin-only (`@admin_required`), CSRF-validated POST;
  annotators get 403, anonymous bounced to sign-in (asserted).
- **File type**: `.csv` extension required; contents must decode as
  UTF-8 (BOM tolerated) — executables/binaries are refused at decode,
  not scanned after the fact. The file is parsed in memory and never
  written to disk, so there is no upload directory to fence.
- **Size caps**: byte cap (IMPORT_MAX_BYTES, default 2 MB) and row cap
  (IMPORT_MAX_ROWS, default 2000), both env-tunable; the app-wide
  16 MB MAX_CONTENT_LENGTH backstops multipart parsing.
- **Structure**: header must be `text` + optional `gold`/
  `expected_output`; extra or misnamed columns refuse the file.
  Row-level rules are identical to the paste flow (MAX_INPUT_CHARS
  caps; blank rows skipped; rows with a missing trailing gold cell are
  handled, a bug the new suite caught and now covers).
- **Duplicates**: in-file duplicates and tasks already present in the
  target project are flagged in the preview (stricter than paste, by
  design).
- **Preview is never trusted**: the confirm step re-validates every
  row from scratch (including n_rows bounds) — a tampered or stale
  preview POST cannot smuggle bad rows in (asserted). Inserts are
  atomic; failure = rollback + flash, nothing half-saved.
- **Audit**: `task.import` records counts + actor only — never file
  contents.
- **Export hardening in the same round**: CSV exports now neutralize
  spreadsheet formula injection (cells starting with `=`, `+`, `-`,
  `@` get Excel's leading-apostrophe defence in `_csv_response`), so
  imported task text can never execute as a formula when an export is
  opened in Excel/LibreOffice.

## System fixes — bank encryption, revision cap, vision exports

**Date:** 2026-09-18 · **Result:** 20/20 suites green (new
tests/test_system_fixes.py, 30 checks).

**1. Bank account numbers encrypted at rest.** The NUBAN is stored as
Fernet ciphertext (AES-CBC + HMAC via `cryptography`, pinned 44.0.1);
plaintext never sits in the column. Key = BANK_ENC_KEY env var or a
SHA-256 derivation of SECRET_KEY (dev-friendly; a dedicated key is
recommended in production so SECRET_KEY rotation stays independent of
bank decryption). A new `bank_account_last4` column powers the masked
Team-page display WITHOUT decryption; the payout CSV is the only
decryption surface (transit-only). Legacy plaintext rows still read
correctly and re-encrypt on the next save. Migration `e91c73a40b57`
widens the column and adds last4. Audit rows and logs never carried
the number; verified unchanged.

**2. Revision loop cap.** A task rejected TASK_MAX_REJECTIONS times
(default 3, env-tunable) auto-returns to the pending pool
(unassigned), the annotator receives a `task.escalated` notification
(bell + email when SMTP is on), the admin flash and the
review.verdict/review.bulk audit rows record the escalation. Bad-fit
pairings can no longer cycle forever; the count is drawn from the
submission history (every rejected submission of that task), so it
survives annotator changes.

**3. Vision-format exports.** Project export now speaks COCO JSON
(schema-valid: info/images/annotations/categories; tasks map to
images with file_name task_<id>.txt and the task text rides in
`brydl_text`; labels become categories; gold answers are never
included in this format) and YOLO text (ZIP: sorted classes.txt, one
label file per task with full-frame placeholder boxes + README
stating the convention, and a JSON manifest mapping task ids to label
files). Both flows keep the existing guarantees: admin-only, CSRF,
audited (format/scope/counts, never contents), pseudonymous ids,
formula-injection-safe CSV unchanged.

**Regression caught by the battery:** the masked-display refactor
initially rendered `****` for annotators with no bank details
(empty-string truthiness); the user-management suite caught it and the
logic now falls back to "Not provided" correctly.

## Consensus engine (2026-09-20) — blindness guarantees

RLHF Phase 2 adds blind multi-annotation rounds. Security-relevant
properties, all suite-tested (test_consensus.py, 32 checks): a single
annotator can never hold two siblings of the same round (claim route
excludes held groups; admin assign skips and warns); annotator
surfaces never render sibling task ids, identities, or verdicts;
arbitration is admin-only + CSRF + audited with task ids only (never
verdict contents beyond the ruling itself); a resolved ruling is
frozen and cannot be altered, and re-arbitration is refused at the
route level. Migration `b2f4c9d61a83`; 22/22 suites green.

## Trusted rendering (2026-09-24) — XSS posture for task content

Task content and model responses are untrusted input. RLHF Phase 3
renders them server-side only: markdown → nh3 allowlist sanitizer →
Pygments CSS highlighting. The rendering suite proves injected
<script>, event handlers and javascript: URLs never reach the
annotator workspace, review detail, or arbitration pages; the
system-check suite now enforces that `|safe` appears ONLY on
sanitizer-backed view variables. No client-side rendering, no
third-party JS, stylesheet self-hosted (offline-first intact).
Battery also caught and fixed a submit-flow regression (success-path
return shadowing the redirect). 23/23 suites green.

## SFT gold-rewrite builder (2026-09-24) — provenance + freeze + audit

RLHF Phase 4 turns finalized work into fine-tuning pairs on an
admin-only surface. Guarantees: every pair carries a validated source
task (provenance never dangles); the machine-harvested completion is
kept immutable next to the editable one and a written note is
mandatory on every deviation; exported pairs are terminal and frozen
so the shipped corpus cannot drift from the database; the export is
POST-only (a GET can never mutate) and flips rows in the same
transaction as its audit event; gold answers never reach annotator
surfaces (asserted in-suite). The battery caught two real holes in
the first cut: SFT routes registered with @admin_required ABOVE
@admin_bp.route silently skipped the role wall (annotators got 200),
and the delete action ignored its CSRF token — both fixed and now
enforced by the system-check security matrix. Audit trail covers
create/update/delete/export. 24/24 suites, 570 checks (clean tally;
Phase 3's "615" was a double-count, true ~517).

## Rubric sliders (2026-09-25) — trusted input for a new field type

RLHF Phase 5 adds per-response rubric sliders to preference tasks.
Security posture: sliders are NATIVE HTML range inputs (zero
client-side logic); every score is re-validated server-side
(InputRequired + NumberRange on the configured scale) so UI tampering
cannot store out-of-range or non-integer values; field names are
index-based so free-text dimension names never reach field ids;
display surfaces (review, arbitration) render stored rubrics only
after strict validation against the project's dimensions; dimension
configuration on the project form is admin-only with length/count
caps flashed on violation. The rubric never participates in
consensus verdict keys, and the JSONL export carries it inside the
validated output document. 25/25 suites, 607 checks.

## Staff role split (2026-09-25) — least privilege for QA and audit

Phase 6 introduces `qa` and `auditor` staff roles under the
same /admin blueprint. Access is least-privilege: QA reviewers get
exactly the review queue, submission detail, bulk review and quality
pages; auditors get exactly consensus arbitration and the audit
trail; money, projects, team management and the SFT corpus stay
admin-only. The role wall is a named decorator per surface
(@qa_required / @auditor_required), enforced at the database by the
widened ck_users_role check, verified by the system-check security
matrix (a POST route without a recognized decorator fails the
battery), and proven per-role in test_roles.py — including that QA
verdicts and auditor rulings are audited under their own accounts.
Sign-in and the financial dashboard both route each role to its own
queue, so privilege boundaries are also the everyday UX.

## Paystack Transfers + NDPR (2026-09-25) — moving money safely

The payout route decrypts bank numbers only inside the admin request
that needs them (same trust boundary as the CSV batch) and never
writes them to logs, flashes or the audit trail. The webhook accepts
NO browser authentication: it verifies the Paystack HMAC-SHA512
signature over the raw body with a constant-time compare (400 on any
failure) and settles exactly one pending payment per reference, so
replayed or forged events cannot double-pay. The system-check CSRF
matrix gained its first documented exemption for this endpoint — the
rule itself stays absolute for every browser-facing POST. The NDPR
notice (/privacy) publishes the controller/processor split, data
inventory, lawful bases, security measures, retention and worker
rights, linked site-wide from the footer. 27/27 suites, 691 checks.

## Client portal (2026-09-25) — credential-as-link threat model

/client/<token> makes Brydl client-facing without accounts. Security
design: the 32-byte urlsafe token is the credential and only its
sha256 is stored (a database leak yields no working links); dead,
deactivated and expired links return 404 without advertising which
case applies; the raw token is redacted from REQ access-log lines,
never reflected into page text (export URLs are prebuilt
server-side), never written to the audit trail, and pages ship
X-Robots-Tag noindex. Deliveries expose approved rows only —
annotator identities, gold answers and reviewer notes are stripped
in code (client_portal.client_safe_record), CSV cells pass the
formula guard. Issue/revoke are admin-only, CSRF-protected and
audited; the security matrix verified the new POSTs after the
decorator-order trap was caught twice more. 28/28 suites, 734
checks.

## Media workspaces (2026-09-26) — untrusted bytes, task-bound delivery

- **Uploads are untrusted input.** Acceptance is by CONTENT, not by
  extension or client content-type: `_sniff_media` reads magic bytes
  (PNG/JPEG/GIF/WebP; RIFF box disambiguates WebP vs WAVE; OggS;
  MP3 ID3/frame-sync; ISO-BMFF ftyp → audio/mp4). **SVG is never
  accepted** — it is an active-content format (script/foreignObject)
  and a stored-XSS delivery vector; sniffing makes extension tricks
  (`evil.png` that is really SVG/text) fail closed. Size capped by
  `MEDIA_MAX_BYTES` (8 MB knob) before parsing; empty uploads
  rejected; `byte_size > 0` also enforced at the schema level.
- **Storage deviation (documented):** bytes live in Postgres
  LargeBinary. Rationale: $0, included in every pg_dump, survives
  redeploys, no second credential surface (no bucket keys). Serving
  reads the row and returns the sniffed allowlist content type —
  never a user-supplied header. Object-store (R2) migration later is
  additive; nothing in the threat model depends on file-system
  paths, so no traversal surface exists.
- **Serving is task-bound.** The annotator route
  `/annotator/task/<tid>/media/<aid>` 404s unless (a) the task's own
  input document references that asset id, (b) the asset belongs to
  the task's project and (c) the task is assigned to the caller —
  guessing ids across tasks or projects yields identical 404s (no
  existence oracle). The staff route (`/admin/media/<id>`) is
  admin/qa/auditor only. Responses carry `Cache-Control: private`,
  so shared/proxy caches must not retain annotator media.
- **Prompt/labels in task docs are untrusted text**: prompts render
  through the same sanitize-allowlist pipeline as every other task
  content (`render_rich`); label choices are parsed server-side and
  the answer form is built from server data only (AnyOf-validated);
  a dangling/broken media reference degrades to the plain-text
  fallback instead of erroring.
- **Dedupe:** sha256 per project with a unique index — identical
  files reuse the row (no storage amplification); the audit entry
  carries only a sha256 prefix, never bytes.
- Dedupe/audit trail: upload flash reuses the existing asset id;
  `media.upload` audits asset id, kind, content type, size and
  sha256[:12].
- Battery: test_media.py (39 checks) covers the sniff matrix, upload
  validation, the full serving auth/scoping matrix, both workspace
  flows, review/arbitration rendering and consensus conflict
  semantics. 29/29 suites, 773 checks, flake8 clean.

## Phase 10 (2026-09-27) — API tier + background jobs

- **Bearer auth only — no browser attack surface.** The API never
  reads the session cookie, never sets cookies, never exposes
  forms; CSRF is impossible by construction (a browser cannot add
  a custom `Authorization: Bearer …` header cross-origin and the
  API never issues a CORS preflight grant that would allow a
  cooperating origin to do so). Missing/unknown/revoked keys are
  uniform 401s (with WWW-Authenticate header) and foreign projects
  are 404s — there is no existence oracle telling an attacker
  which project ids belong to which keys.
- **Key storage is show-once, sha256 only** — the same temporary
  password contract as client-portal links and API password-reset
  tokens. The key flashes once in the admin console, never renders
  again; the audit table records the `brydl_xxxx…` prefix, never
  the raw value. Revocation is instant and effective: a revoked
  key returns 401 on its very next request (no caching beyond the
  request).
- **Rate limiting is per-key, per minute.** `kvstore.bump_window`
  uses Redis when `REDIS_URL` points at a live Redis (Render Key
  Value, or any Redis) and falls back to an in-process counter
  when it is not. A counter failure (Redis outage) never blocks
  requests. The default 60/min is conservative enough for a lab
  integration; raised by `API_RATE_LIMIT_PER_MINUTE`.
- **Result payloads share the client_portal contract.**
  `client_safe_record` is reused verbatim: annotator identity, gold
  expected_output, reviewer notes and internal statuses never
  leave the server — identical rule, identical tests. POST bodies
  are validated length-capped, fail-closed, and batched under the
  same `UPLOAD_BATCH_LIMIT` as CSV imports (no new attack surface).
- **Jobs carry identifiers, never secrets.** No bank account data,
  no raw API tokens, no Fernet material, no SMTP passwords, no
  Paystack secret is ever enqueued. Payload rows are bounded to
  short JSON dicts of ids (project id, task id, email address,
  transfer reference). Email bodies may be user-generated (same
  input path as the existing inline SMTP call) but they leave the
  app to SMTP exactly like before; the job moves the call off the
  request, it does not change what is sent.
- **Settlement idempotency is preserved.** The Paystack reconcile
  runner uses the *same* pending-guard the webhook uses: it
  refreshes each payment row and only mutates rows whose status is
  still 'pending', so a late webhook arriving mid-sweep or a
  re-sweep minutes after a settle is a no-op. The runner also
  refreshes after network errors before committing.
- **Race-safe claiming.** The conditional UPDATE (status='queued'
  → 'running', attempts+1) guarantees one poller wins per row; a
  second poller's UPDATE matches zero rows and it moves on.
  Combined with `_requeue_stuck` at boot, no crash or redeploy
  leaves a job lost or double-run. Email deliveries are the only
  possible double-send in a crash-between-claim-and-send window —
  SMTP failures are raised so the runner retries, duplicates are
  rare and explicitly acceptable; no financial action has the
  double-send risk (payouts are pending-guarded and idempotent by
  reference).
- **Kill switches are honoured.** Setting `JOBS_ENABLED=0` in env
  (or the test battery's `BRYDL_DISABLE_JOBS=1`) keeps the runner
  thread from ever starting.

## Qualification quizzes (2026-09-26) — gate integrity + answer-key secrecy

- **The answer key never leaves the server.** Correct flags live
  only in the stored canonical payload; the annotator form carries
  option TEXTS as radio values, and grading runs server-side. There
  is no client-side check, hidden field, or JS bundle that could
  leak which option is right.
- **Gate coverage is total on the worker paths.** Self-claim
  (oldest-task query excludes gated projects), admin assignment
  (gated tasks reported as skipped, never force-assigned) and the
  task page (redirect to the quiz; unknown/unowned tasks stay 404)
  all enforce the same predicate: active quiz AND no passed/exempt
  attempt. Draft/retired quizzes gate nothing, so the gate is always
  explicit state, never an accident.
- **Attempts are bounded and honest.** max_attempts counts failed
  tries server-side; a pass is terminal (retake refused); the
  exhausted path points to support, not to a reset an impatient
  annotator could farm.
- **Exemptions are first-class audit events** (`quiz.exempt`,
  `quiz.revoke`): an exempt row carries no answer sheet and records
  the admin who vouched; revocation removes passed/exempt rows while
  keeping failed history (the attempts counter stays honest).
- **Tamper surface:** quiz content is admin-authored (trusted staff)
  but still rendered escaped by Jinja defaults; the paste parser
  rejects malformed blocks fail-closed and the console refuses to
  edit an active quiz (no mid-flight answer-key changes under
  annotators holding attempts). Attempt POSTs are CSRF-protected and
  role-gated like every other annotator route.
- Battery: test_qualification.py (52 checks) — 30/30 suites, 825
  checks, flake8 clean.

## Independent vulnerability sweep (2026-09-25)

Full-surface audit: dependency CVE scan (pip-audit), code-pattern
sweep (raw SQL, redirect targets, file reads, debug surfaces, cookie
flags, security headers, CSV formula injection, reset-token flow,
lockout bounds, session invalidation) and live black-box probes
(anonymous access matrix, CSRF enforcement, IDOR, path traversal,
malformed ids, error-page leakage).

- FIXED: cryptography 44.0.1 carried 10 known advisories
  (PYSEC-2026-2141/-35/-3552/-3553/-3554, GHSA-537c-gmf6-5ccf — the
  Fernet bank-encryption dependency). Bumped to 50.0.1; pip-audit
  now reports zero known vulnerabilities across the pinned tree.
- VERIFIED CLEAN (no action): root-relative-only redirect targets
  (scheme + netloc both rejected); CSV export formula-injection
  defence (= + - @ cells get a leading apostrophe); reset tokens
  stored sha256-hashed with TTL + single-use + auth_version bump;
  escalating email+IP login lockout behind generic errors and a
  timing-equalising dummy hash; 16 MB request cap; HttpOnly/SameSite
  cookies with Secure on Render; CSP, X-Frame-Options DENY,
  nosniff, Referrer-Policy, Permissions-Policy and Render-gated
  HSTS on every response; debug server bound to __main__ only
  (gunicorn in prod); no raw SQL in routes (ORM only); no secrets
  in repo (.env.render.example placeholders, .gitignore covers
  .env + instance/); 500 handler never leaks tracebacks; live
  probes all passed — anonymous users redirected to login on every
  protected route, POSTs without CSRF tokens rejected, annotators
  cannot open other annotators' tasks (404), cross-role walls hold
  (annotator 403 on /admin), static path traversal blocked,
  malformed/unknown ids return branded 404s, GET /logout refused.
- RESIDUAL RISKS (accepted, documented): login lockout state is
  in-process (single gunicorn worker) so distributed guessing is
  bounded only per email+IP pair; SECRET_KEY/BANK_ENC_KEY fallbacks
  generate local keys for dev — production must set both explicitly
  in the Render environment (checklist item). CSV formula defence
  covers the OWASP lead characters; exports opened in non-
  spreadsheet tools are unaffected either way.
