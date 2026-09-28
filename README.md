# Brydl Dashboard

A production-ready **AI data-labeling management dashboard**: admins create
projects, upload and assign tasks; annotators complete them; QA reviews every
submission with scores and feedback; earnings are tracked and paid out —
with a full audit trail, structured logs, database backups, and a test
battery covering security, correctness, and scalability.

**Stack:** Python 3.11+ · Flask 3 · SQLAlchemy 2 (SQLite now, PostgreSQL
ready) · Flask-Login + Werkzeug hashing · Flask-WTF (CSRF everywhere) ·
Bootstrap 5 via CDN · gunicorn on Render.

---

## Features

- **Bank details encrypted at rest**: NUBANs are stored as Fernet
  ciphertext (`BANK_ENC_KEY`, or derived from SECRET_KEY); the Team
  page shows last4 only — decryption happens solely for the payout
  CSV.
- **Revision-loop cap**: after 3 rejections of the same task
  (`TASK_MAX_REJECTIONS`) it auto-returns to the pool; annotator is
  notified, admins see it in flash + audit.
- **Trusted rendering**: task content (markdown, code, multi-turn
  conversations) renders server-side with syntax highlighting and
  chat bubbles — sanitized against a strict allowlist, fully
  self-hosted (no CDN, no client-side JS).
- **Client portal**: a private, revocable link lets each client
  follow their project's progress and download approved deliverables
  (JSONL/CSV) — no account, no annotator identities, no gold leaks.
- **Media workspaces**: image-labelling and audio-transcription
  tasks — staff upload files to a per-project library (magic-byte
  sniffed, size-capped, sha256-deduped); annotators see the picture
  or audio player right in the task; QA and arbitration render the
  same media. Bytes live in Postgres (in every backup), served
  strictly task-bound.
- **Machine API**: project-scoped Bearer keys let the client's
  lab push tasks in and pull approved deliverables out, with the
  same client-safe contract as the portal export and per-key rate
  limits.
- **Background jobs**: a DB-backed queue moves SMTP delivery and
  Paystack reconciliation off the request path, runs inside the
  web process today, and scales to a dedicated worker without code
  changes. Redis is optional for shared counters.
- **Qualification quizzes**: gate any project with a short
  multiple-choice quiz the admin authors in minutes — annotators
  pass it before claim or assignment hands them that work, with
  attempt caps, audited exemptions, and a qualified roster. The
  answer key never leaves the server.
- **One-click payouts**: optional Paystack Transfers send Naira to
  annotator bank accounts from the Payments page (signature-verified
  webhooks settle them idempotently); the bank-batch CSV flow remains
  as the offline fallback.
- **Role-split staff sites**: dedicated QA reviewers work the
  submission-review queue; auditors own consensus arbitration and
  the audit trail — each role lands directly in its workspace, with
  the financial console admin-only.
- **Rubric sliders**: preference projects can define rating
  dimensions; annotators score both responses with native sliders
  (validated server-side, no client-side logic).
- **SFT dataset builder**: harvest finalized work (binding
  consensus rulings, approved submissions, gold keys) into
  supervised-fine-tuning pairs with a mandatory written note on
  every human rewrite, and export frozen chat-messages JSONL.
- **RLHF blind consensus**: set annotations-per-task to 2 or 3 and
  every task is worked blind by that many annotators; agreement is
  detected automatically, disagreements go to your Conflicts view for
  a binding, audited ruling; exports carry the consensus outcome.
- **Vision exports**: project export speaks COCO JSON and YOLO text
  (ZIP: classes.txt + per-task labels + manifest) alongside JSONL and
  CSV.
- **Tier 1 operations**: annotators *Claim next task* from the pending
  pool (race-safe, per-day cap), a notification bell (assigned /
  rejected / payout events, in-app + email when SMTP is set), project
  deadlines (form → projects list → annotator queue), and an admin
  **Quality** dashboard (approval rate, avg QA score, gold accuracy
  per annotator).
- **CSV task import (from device)**: Tasks tab → *Import from file* —
  upload a `.csv` (`text` column + optional `gold`), get an editable
  preview that flags empty/oversize/duplicate rows, fix rows right in
  the table, then import only what you ticked. Server re-validates
  everything on save; files are parsed in memory only (never on disk);
  exports neutralize spreadsheet formula injection.
- **Annotator payout accounts**: annotators enter bank name, account
  type (savings/current), 10-digit NUBAN and account name on their
  Profile page (plus an optional contact phone). Masked on the Team
  page and in full in the bank-batch CSV. Audited, CSRF-only.
- **Schema drift guard**: an old local database fails at boot with the
  exact fix instead of cryptic 500s on every page.
- **Offline-first annotators (PWA)**: installable to the home screen;
  typed answers autosave on-device until submitted; fully styled pages
  and a branded offline fallback on dead connections.

| Area | What you get |
| --- | --- |
| Auth | Role-based access (`admin` / `annotator`), 7-day sessions, lockout after repeated failed sign-ins, POST-only logout |
| Projects | Create/edit, task types (classification, NER, translation, RLHF), pay-per-task in USD, pause/resume |
| Tasks | Bulk paste upload (atomic batches, 500 cap), filters, bulk assign, unassign, 10k-char per-task cap |
| Annotation | Per-task-type forms, revision loop for rejected work, live character counters, Ctrl+Enter submit |
| QA review | Queue with project/annotator filters, side-by-side gold-vs-answer comparison, score 0–100 + notes, bulk verdicts, race-safe double-review protection |
| Payments | Earnings ledger (USD + Naira display), payout generation with claim links (no double payment), mark-paid with method + timestamp |
| Observability | `audit_logs` table (who/what/when/IP for every action) browsable at `/admin/audit`, correlated request-id logging, `X-Request-ID` header |
| Ops | `flask backup` CLI (create/list/verify/restore, checksummed), Render Blueprint, security headers + CSP, custom error pages |

---

## Project structure

```
brydl-dashboard/
├── app.py               # App factory, security headers, error pages, request logging
├── config.py            # ALL settings (env-overridable) — the single tuning surface
├── models.py            # User, Project, Task, Submission, Payment, AuditLog (+ money() helper)
├── extensions.py        # db, login_manager, SQLite pragmas (foreign_keys, WAL)
├── audit.py             # log_event(): stdout log + audit_logs row per action
├── backup.py            # flask backup create|list|verify|restore
├── seed_admin.py        # flask seed-admin (first-admin bootstrap)
├── seed.py              # DEV-ONLY demo data (refuses to run on Render)
├── routes/
│   ├── auth.py          # /login /logout
│   ├── admin.py         # dashboard, projects, tasks, review, payments, audit
│   ├── annotator.py     # dashboard, task page, history
│   ├── public.py        # / landing page (public; authed users → dashboard)
│   └── (auth.py: /account/theme · annotator.py: profile, terms gate, continue)
├── templates/           # Jinja2 + Bootstrap 5 (incl. errors/, landing, _boot,
│   │                    #   annotator/profile + terms, light/dark themes)
├── static/css|js        # design layer + progressive enhancements (CSP-safe)
├── static/sw.js         # service worker: offline page + asset caching
├── static/brand/        # logo (recreated) + landing hero cover image
├── migrations/          # Alembic package: production schema authority
├── mailer.py            # smtplib mailer (password-reset links; log fallback)
├── tests/               # 16 end-to-end suites + run_all.sh
├── Procfile             # web: gunicorn --threads 4 "app:create_app()"
├── render.yaml          # Render Blueprint (free plan)
├── runtime.txt          # python-3.11.6
└── SECURITY_AUDIT.md    # threat model, fixes, design decisions
```

---

## 1. Run it locally (step by step)

**Prerequisites:** Python 3.11 or newer (`python3 --version`). No database
server needed — SQLite is created automatically.

```bash
# 1. Get the code (however you copied it), then enter the folder
cd brydl-dashboard

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create demo data (DEV ONLY — refuses to run on Render)
python seed.py
#    → 1 admin + 5 annotators + 1 project + 10 tasks
#    → creates instance/brydl.db (the whole database is this one file)

# 5. Start the development server
python app.py
#    → http://127.0.0.1:5000
```

**Sign in (seeded credentials — dev only):**

| Role | Email | Password |
| --- | --- | --- |
| Admin | `admin@brydl.com` | `admin123` |
| Annotators | `annotator1@brydl.com` … `annotator5@brydl.com` | `annotator123` |

> Re-running `python seed.py` is safe — it skips anything that already
> exists. **Change the admin password before any real use** (Profile is on
> the roadmap; see "Reset a password" in the runbook below).

### Take the workflow for a spin

1. As **admin**: Projects → *Create Project* → Tasks → open **Upload Tasks**,
   paste one task per line (optional gold answer after a TAB) → tick tasks →
   **Assign** to an annotator.
2. Sign out, sign in as **annotator1@brydl.com**: dashboard shows the task →
   open it → submit an answer.
3. Back as **admin**: Review → open the submission → compare against gold →
   set a score → **Approve** (or **Reject** with "leave for revision").
4. Payments → see the earnings ledger → **Generate Payments** → **Mark paid**
   (method + timestamp recorded).
5. Audit → every action you just did, newest first.

---

## 2. Configuration (all optional)

Every setting lives in `config.py` and can be overridden with environment
variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | persisted random key in `instance/` | Sessions + CSRF. **Set a strong one in production** (`python -c "import secrets; print(secrets.token_hex(32))"`). Refuses to boot if shorter than 32 chars. |
| `DATABASE_URL` | `instance/brydl.db` (SQLite) | Point at PostgreSQL on Render (`postgres://` is auto-rewritten). |
| `USD_TO_NGN_RATE` | `1600` | Display-only Naira conversion (Decimal). |
| `PAGINATION_PER_PAGE` | `20` | Page size for every list view. |
| `BRYDL_AUTO_SCHEMA` | `1` | `1` = dev: create tables at boot. Render sets `0`: schema comes only from `flask db upgrade`. |
| `BRYDL_TERMS_VERSION` | `1.0` | Bump to re-prompt every annotator to accept the new Terms. |
| `SMTP_HOST` / `SMTP_PORT` | unset / `587` | Outbound mail. Unset = reset links are written to the app log instead of emailed (dev). |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | unset | SMTP auth (e.g. a provider mailbox/API key). |
| `MAIL_FROM` | `Brydl <no-reply@…>` | Envelope sender for reset emails. |
| `SMTP_STARTTLS` | `1` | STARTTLS on 587; port 465 uses implicit TLS automatically. Set `0` to disable. |
| `RESET_TOKEN_MAX_AGE_SECONDS` | `3600` | Reset links expire after one hour. |
| `FORGOT_MAX_PER_WINDOW` / `FORGOT_WINDOW_SECONDS` | `3` / `900` | Max reset emails per email address per 15 minutes (anti email-bombing). |
| `UPLOAD_BATCH_LIMIT` | `500` | Max tasks per upload/assign/bulk-review. |
| `MAX_TEXTAREA_CHARS` | `10000` | Cap for one task/answer/instructions. |
| `LOGIN_MAX_FAILURES` / `LOGIN_LOCKOUT_BASE_SECONDS` / `LOGIN_LOCKOUT_MAX_SECONDS` | `5` / `120` / `900` | Sign-in throttle ladder. |
| `BACKUP_DIR` / `BACKUP_KEEP` | `backups/` / `14` | Where snapshots go / how many are kept. |
| `LOG_LEVEL` | `INFO` | Stdout log verbosity. |

`RENDER` (set automatically by Render) switches on Secure cookies, HSTS and
proxy-header handling — never set it locally.

---

## 3. Run the tests

The `tests/` folder holds 16 end-to-end suites (annotator workflow,
annotator experience, QA review, task management, payments, hardening,
audit trail, deployment files, scalability, brand/landing, export kit,
migrations/PostgreSQL, offline PWA, password reset). They use a
throwaway database.

> **Upgrading an existing local database?** From now on schema changes
> ship as migrations. A database created by an older release: delete
> `instance/` and re-run `python seed.py` (pre-production data only).
> To adopt migration tracking on a database already at the current
> schema: `flask --app app db stamp head`. To rehearse the production
> path anywhere: `BRYDL_AUTO_SCHEMA=0 DATABASE_URL=sqlite:////tmp/x.db
> flask --app app db upgrade`.

> ⚠️ **The suites wipe `instance/` and `backups/`.** Run
> `flask --app app backup create` first if your local data matters.

```bash
source .venv/bin/activate
bash tests/run_all.sh
# or explicitly:
PYTHON=.venv/bin/python bash tests/run_all.sh
```

Each suite prints per-section progress; the runner stops at the first
failure and prints its tail. Expect ~20–30 seconds total.

### Full verification report (security · validation · features · stress)

For a deep sweep with performance metrics, run the one-shot verifier —
it executes 36 additional conditions (auth/session attacks, XSS/SQLi
probes, CSRF matrix, every validation boundary, full feature
round-trips, race safety under 12 concurrent submitters) plus latency
percentiles, throughput, bulk-operation timings and database metrics,
then writes everything to `TEST_REPORT.md`:

```bash
python tests/full_report.py          # ~10s → TEST_REPORT.md
```

---

## 4. Back up and restore

The whole database is one file, and `backup.py` snapshots it safely while
the app is running:

```bash
flask --app app backup create          # snapshot + sha256 manifest + prune
flask --app app backup list            # what exists (sizes, checksums, rows)
flask --app app backup verify brydl-20260910-120000.db
flask --app app backup restore brydl-20260910-120000.db          # dry-run
flask --app app backup restore brydl-20260910-120000.db --yes   # do it
```

Snapshots land in `backups/` (gitignored) with a `.manifest.json` sidecar.
**Restore verifies the checksum first, replaces the file atomically, and
tells you to restart the web service** — running workers keep the old file
until they reconnect. Backups are audited (`system.backup`,
`system.restore` in `/admin/audit`).

**Habit for the pilot:** create a backup before every deploy and copy the
`backups/` folder off the machine (cloud drive / USB) — see the Render
caveat below.

---

## 5. Deploy to Render (step by step)

> **First time deploying?** `DEPLOY_GUIDE.md` in the repo root is the
> longer click-by-click walkthrough (GitHub account + PAT auth, push,
> PostgreSQL provisioning, admin, backups, the 30-day free-DB clock).

### Step 0 — know the free-tier trade-off

The Blueprint provisions **PostgreSQL**, so data survives deploys and
restarts. The one free-tier catch: **Render's free PostgreSQL expires
after ~30 days** (contents are then deleted). Do the deploy rehearsal on
it, but upgrade the database plan (Render dashboard → the database →
Change plan, e.g. `basic-256mb` ≈ $6/mo) before real annotator work and
payroll go in. Back up either way — `flask --app app backup create`
locally (SQLite), `pg_dump` against production (see DEPLOY_GUIDE.md
§3.2).

### Step 1 — push the code to GitHub/GitLab

```bash
cd brydl-dashboard
git init
git add .
git commit -m "Brydl Dashboard v1"
git branch -M main
git remote add origin https://github.com/<you>/brydl-dashboard.git
git push -u origin main
```

`.gitignore` already excludes `instance/`, `backups/`, `.env`,
`__pycache__/`, `venv/` — never commit the database or secrets.

### Step 2 — create the service

**Easiest (Blueprint):** Render dashboard → **New + → Blueprint** → pick the
repository → **Apply**. Render reads `render.yaml` and provisions BOTH the
PostgreSQL database (`brydl-postgres`) and the web service: build
`pip install -r requirements.txt && flask db upgrade`, start
`gunicorn --threads 4 "app:create_app()"`, generated `SECRET_KEY`,
`FLASK_ENV=production`, `DATABASE_URL` auto-injected, health check on
`/login`.

**Manual equivalent:** New + → PostgreSQL first (name it `brydl-postgres`)
→ then New + → Web Service → pick the repo → Environment
**Python 3.11.6** → build/start commands as above → env vars
`SECRET_KEY` (generate), `FLASK_ENV=production`, `DATABASE_URL` (the
database's *Internal Database URL*), `BRYDL_AUTO_SCHEMA=0` → Create.

Every `git push` to `main` now auto-deploys, and each build runs
`flask db upgrade` — the versioned migration in `migrations/` is the
production schema authority (never `create_all`).

### Step 3 — create the first admin

Free web services have no shell, so create the admin **from your machine
against the production database** (needs the database URL from Step 4, or
do this step right after upgrading):

```bash
source .venv/bin/activate            # psycopg2-binary is already in requirements.txt
export DATABASE_URL="postgresql://...render.com/brydl"   # Render's Internal/External URL
flask --app app seed-admin --email you@brydl.ng
# prompts for a password (min 8 chars); or non-interactive:
# SEED_ADMIN_PASSWORD=... flask --app app seed-admin --email you@brydl.ng
```

The CLI refuses if an admin already exists — additional admins (and all
annotator accounts) are created from the app's **Team** page, where you
can also deactivate leavers (kills their sessions instantly) and reset
passwords (one-off password shown once).

### Step 4 — before the 30-day free-DB expiry

1. Render dashboard → **brydl-postgres** → **Change plan** → e.g.
   `basic-256mb` (data carries over; the service reconnects on the next
   deploy — no code or config changes).
2. Snapshot before touching it: `flask --app app backup create` from
   your machine with `DATABASE_URL` set (§4).
3. Future schema changes ship as Alembic migrations: edit `models.py` →
   `BRYDL_AUTO_SCHEMA=0 DATABASE_URL=<render-url> flask --app app db
   migrate -m "describe it"` locally against a scratch copy → commit →
   the next deploy's build applies it automatically.

### Step 5 — verify the deployment

- Open `https://<your-service>.onrender.com/login` — sign in as admin.
- Deliberately visit a bad URL → you should see the branded 404 page.
- Check **Logs** in the Render dashboard: every request logs as
  `REQ GET /... -> 200 (12 ms) ip=... user=...` with a `[request_id]`; a
  user-reported error's `X-Request-ID` finds its exact log slice.
- Do an action (create a project) → confirm it appears in **Audit**.

---

## 6. Operations runbook

| Task | How |
| --- | --- |
| Watch logs | Render dashboard → Logs (or `render logs` CLI). Format: `TIME LEVEL [request-id] app: message`. |
| Investigate an error | Take the `X-Request-ID` from the user's report/page, grep the logs for it — you get the access line, every audit line, and the traceback of that one request. |
| Who did what | Sign in as admin → `/admin/audit` → filter by actor or action (`payment`, `login`, …). |
| Back up before deploying | `flask --app app backup create` (local), download `backups/`. |
| Reset any password | Admin → **Team** → *Reset password* (one-off password, shown once, audited). Annotators can also self-serve via **"Forgot your password?"** on the login page (emailed one-hour single-use link; needs `SMTP_HOST` set, otherwise the link lands in the app log). |
| Export a client dataset | Admin → **Projects** → *Export* → pick JSONL/CSV and scope. Approved-only + no gold answers by default; every export is audited (row counts, never content). |
| Run payroll | Annotators add their payout account on their profile (bank, account type, 10-digit NUBAN, account name). Admin → **Payments** (rows missing details are flagged) → generate payout runs → *Bank batch CSV (pending)* downloads bank-ready rows incl. account details → execute → mark paid. |
| "Invalid email or password" but I'm sure | Account may be deactivated or you're locked out (5 failures → 2–15 min). The audit page shows the reason (`unknown_email` / `bad_password` / `inactive_account`). |
| App 500s at boot on Render | `SECRET_KEY` set but shorter than 32 chars, or `DATABASE_URL` malformed — the log says exactly which. |
| Styles look broken offline | Fixed by the service worker: after one online load the pinned CDN CSS/JS is cached and pages stay fully styled offline; failed page loads get the branded offline page; typed answers autosave on-device until submitted. |

---

## 7. Security & design notes

- CSRF on every form, security headers + strict CSP (no inline scripts),
  parameter-safe filters, one-hash-timing login, deactivation kills live
  sessions, DB-level CHECK constraints as the last line of defence.
- Money is `Decimal` end to end (`Numeric(10,2)` columns) — no float drift.
- Full history: `SECURITY_AUDIT.md` documents every threat, fix, and the
  WAL-aware backup/restore design.

## 8. Roadmap

Paystack/Flutterwave bulk transfers · self-hosted Bootstrap (the
service worker already covers offline styling) · live FX rate for the
Naira display.
