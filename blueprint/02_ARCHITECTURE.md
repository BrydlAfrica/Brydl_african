# 02 — Architecture

## File tree (complete, excluding `static/vendor/` binaries)

```
brydl-dashboard/
├── app.py                  # Application factory; extensions; error handlers;
│                           #   security headers; blueprint registration;
│                           #   boot guard (schema drift → loud RuntimeError)
├── config.py               # All env knobs + engine options (SQLite creator
│                           #   with connect-retry; PG connect_timeout=10);
│                           #   SECRET_KEY fallback chain (env → instance file)
├── extensions.py           # db, migrate, login_manager, csrf instances
├── models.py               # ALL 7 models + money() normalizer + utc_now()
├── audit.py                # log_event(): tamper-evident audit trail writer
├── backup.py               # SQLite backup helper (dev)
├── mailer.py               # send_email() + send_notification_email();
│                           #   no-op-safe when SMTP unset (dev: log link)
├── notifications.py        # notify(): in-app row + optional email; never raises
├── rendering.py            # trusted rendering of untrusted content:
│                           #   markdown→HTML + nh3 allowlist sanitizer +
│                           #   Pygments highlighting + chat turns
├── sft.py                  # SFT gold-rewrite pipeline (Phase 4):
│                           #   harvest finalized work into training
│                           #   pairs + JSONL export; pure, never raises
├── rubric.py               # rubric dimension parsing + score
│                           #   validation (Phase 5); pure
├── paystack.py             # Paystack Transfers client (Tier 2):
│                           #   stdlib HTTP, never raises; webhook
│                           #   HMAC verification
├── api_keys.py             # Bearer API key helpers (Phase 10):
│                           #   generate + hash for project-scoped
│                           #   machine keys (show-once, hash-only).
├── kvstore.py              # optional Redis client (Phase 10):
│                           #   shared counters with in-process
│                           #   fallback when Redis is unreachable.
├── jobs.py                 # DB-backed background job queue
│                           #   (Phase 10): in-process runner,
│                           #   conditional-update claiming,
│                           #   exponential backoff retries.
├── quizzes.py              # qualification quiz engine (Phase 9):
│                           #   paste-format parser, server-side
│                           #   grading, deterministic order
├── client_portal.py        # client portal helpers (Phase 7):
├── media in routes/admin.py + routes/annotator.py  # Phase 8:
│                           #   magic-byte sniffing, task-bound
│                           #   serving, in-DB bytes
│                           #   token hashing, log redaction,
│                           #   client-safe delivery records
├── security_crypto.py      # Fernet encrypt/decrypt for bank numbers
├── seed.py                 # Demo project/task data (dev)
├── seed_admin.py           # Bootstrap the admin account (prod-safe)
├── requirements.txt        # Pinned; cryptography for bank encryption
├── Procfile                # web: gunicorn "app:app" (preload, timeout 120)
├── render.yaml             # Render DB + web service + env blueprint
├── runtime.txt             # python-3.13.x
├── .env.render.example     # Every production env var, documented
├── migrations/versions/    # 8 linear migrations (see 03_DATA_MODEL.md)
├── routes/
│   ├── __init__.py
│   ├── public.py           # landing, manifest, service worker, offline page
│   ├── auth.py             # login/logout/theme/forgot/reset
│   ├── admin.py            # the operator console (26 routes)
│   └── annotator.py        # the annotator workspace (13 routes)
├── templates/
│   ├── base.html           # shell: navbar w/ role-aware nav + notif bell,
│   │                       #   flash region, self-hosted BS5+Icons, theme
│   ├── _boot.html          # pre-base boot twig (drift guard message)
│   ├── landing.html, login.html, forgot_password.html, reset_password.html
│   ├── offline.html        # PWA fallback
│   ├── errors/403|404|405|500.html
│   ├── admin/  dashboard, projects, project_form, project_export,
│   │           tasks, tasks_import_preview, review, review_detail,
│   │           quality, payments, users, audit  (.html each)
│   └── annotator/ dashboard, tasks(→history), task, history,
│                   notifications, profile, terms  (.html each)
├── static/
│   ├── css/style.css       # brand + theme layer on top of Bootstrap
│   ├── css/pygments.css    # Pygments token colors (generated, self-hosted)
│   ├── js/main.js          # theme toggle, flash helpers, PW prompts
│   ├── sw.js               # service worker (offline-first cache)
│   └── vendor/             # bootstrap.min.css, bootstrap.bundle.min.js,
│                           #   bootstrap-icons.min.css, fonts/woff{,2}
├── tests/                  # 20 suites (see 10_TESTING.md)
├── docs: README, ROADMAP, DEPLOY_GUIDE, POST_DEPLOY_CHECKLIST,
│         SECURITY_AUDIT, TEST_REPORT, BUILD_REPORT
└── blueprint/              # THIS specification
```

## Application factory flow (`create_app`)

1. Flask app; load `config.Config`.
2. Init `extensions` (db, migrate, login_manager, csrf).
3. **Boot guard:** when `BRYDL_AUTO_SCHEMA=0` (production), verify the
   database is at the latest Alembic revision; a stale DB raises a loud
   `RuntimeError` naming the exact remediation (`flask db upgrade`).
   `flask db` CLI commands bypass the guard.
4. When `SCHEMA_AUTO_CREATE=1` (local default), `db.create_all()` on an
   empty DB.
5. Login manager: `login_view="auth.login"`; **fail-closed user_loader**
   (DB error ⇒ load fails ⇒ user is anonymous, never elevated);
   `session_protection="strong"`.
6. Register blueprints: `public` at `/`, `auth` at `/`,
   `admin` at `/admin`, `annotator` at `/annotator`.
7. Error handlers render branded 403/404/405/500 pages.
8. After-request: security headers (see 06_SECURITY.md).
9. `app.logger` request-line format includes a request id and the
   acting user id.

## Blueprint conventions

- Every module defines its routes with explicit `endpoint=` names where
  paths contain parameters; URL helpers are always used (`url_for`).
- **Guards:** `@admin_required` / `@annotator_required` decorators wrap
  every protected route (login + role + active check). The annotator
  blueprint adds a `before_request` **terms gate**: an annotator who
  has not accepted `TERMS_VERSION` is 302-redirected to
  `/annotator/terms` from every route except the terms pages.
- Admin never reaches annotator pages and vice versa (403).
- Annotator blueprint exposes an `app_context_processor` providing
  `unread_notifications` for the navbar badge (failure ⇒ 0, never an
  error page).

## Self-hosted front-end (offline-first)

- Bootstrap 5.3.8 CSS + JS bundle and Bootstrap Icons 1.13.1 (CSS +
  woff/woff2 fonts) live in `static/vendor/` and are served locally.
  **No CDN at runtime.** Upgrading = replace the vendored files.
- PWA: `manifest.webmanifest` + `sw.js` cache the shell + vendor
  assets; `/offline` renders when navigation fails; theme (light/dark)
  persisted per-user (`users.theme`) and applied on `base.html`.

## Conventions that matter for a faithful rebuild

- UTC everywhere: `models.utc_now()` is the single time source.
- Pagination: `db.paginate(..., per_page=20, error_out=False)`;
  out-of-range pages redirect to the last valid page.
- Streaming/`yield_per` for exports and large reads
  (`config.EXPORT_YIELD_PER`).
- `os.path.join()` for every filesystem path; instance dir for the
  SQLite file and the generated `secret_key` fallback file.
- Flash messages always use the four Bootstrap categories
  (success/info/warning/danger) and are HTML-escaped by Jinja — tests
  that grep flashes must grep escaped text.
