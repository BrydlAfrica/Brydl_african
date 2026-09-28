# 09 — Deployment & operations

## Topology (Render, free/low tier)

- **Web service** `brydl-dashboard`: GitHub auto-deploy on push;
  Build `pip install -r requirements.txt`; Start
  `gunicorn app:app --preload -w 2 --timeout 120` (Procfile);
  `PYTHON_VERSION` from runtime.txt.
- **PostgreSQL** `brydl-postgres` (Oregon region): db `brydl`, user
  `brydl_user`. Internal URL (`-a` flag) works **only inside** Render
  services; the external URL (`?sslmode=require`) is for the laptop.
- Env vars on the service: `DATABASE_URL` (auto), `SECRET_KEY`,
  `FLASK_ENV=production`, `BRYDL_AUTO_SCHEMA=0`, `PYTHON_VERSION`,
  optional `SITE_URL`, `BANK_ENC_KEY`, `CLAIM_DAILY_CAP`,
  `TASK_MAX_REJECTIONS`, `USD_TO_NGN_RATE`, SMTP_* (deferred).

## Deploy procedure (the one that works)

1. Commit + push from the local repo
   (`E:\Brydl\BrydlAfricanV1\brydl-dashboard`, venv `apex-env`,
   Windows cmd; GitHub user `brydldata-lab`).
2. Render auto-redeploys on the new commit.
3. Migrations apply via boot (`BRYDL_AUTO_SCHEMA=0` + boot runner
   executing `flask db upgrade` before gunicorn binds) — a new
   migration lands automatically with the deploy.
4. Verify: `/` loads, admin login works, new feature spot-checked,
   logs show the boot guard passing and `N upgrade(s) applied`.

## First-provision (fresh account) order

1. Create the Postgres instance **in Oregon** (never Frankfurt — see
   region note), copy the internal DB URL.
2. Create the web service with the repo, set env vars
   (`SECRET_KEY`, `FLASK_ENV=production`, `BRYDL_AUTO_SCHEMA=0`),
   attach the database (Render injects `DATABASE_URL`).
3. First deploy runs the 8-migration chain on the empty DB.
4. Bootstrap the admin: `seed_admin.py` via the Render shell (or set
   the env-driven bootstrap) → admin user id #1, audited.
5. Add `SITE_URL` etc. → redeploy.

## Operational procedures

- **DB credential rotation** (Render's current flow, per
  render.com/docs/postgresql-credentials): database Info page →
  Credentials → **+ New default credential** → switch the web
  service's `DATABASE_URL` to the new internal connection string →
  redeploy → verify the app → DELETE the old credential so the
  leaked password stops working entirely. Downtime ≈ one redeploy.
- **Backups:** weekly manual `pg_dump` via the external URL from the
  laptop, stored off-device (procedure in POST_DEPLOY_CHECKLIST.md);
  UptimeRobot ping recommended.
- **Terms update:** bump `BRYDL_TERMS_VERSION` env → all annotators
  get re-prompted on next request.
- **Bootstrap/font upgrade:** replace files in `static/vendor/` —
  nothing else changes (no CDN).

## Known environment facts & cautions (do not lose)

- The repo `render.yaml` still contains **two `region: fra` lines**
  (operator's laptop copy is fixed) — remove on next touch. The live
  DB/service are in **Oregon**; NEVER recreate in Frankfurt (data +
  latency).
- SMTP verified end-to-end 2026-09-20 (reset-link email received);
  notification emails are live. If delivery ever fails, first stop is
  the service logs (mailer never crashes the app on SMTP errors).
- The in-app preview in chat sandboxes cannot reach external
  resources — irrelevant to production but explains any local preview
  degradation.

## Escalation/history pointers

DEPLOY_GUIDE.md (step-by-step first deploy), POST_DEPLOY_CHECKLIST.md
(verification list), SECURITY_AUDIT.md (dated security ledger),
ROADMAP.md (tier plan: Tier 1 shipped; Tier 2 Paystack/client
portal/NDPR; Tier 3 media/consensus/FX/avatars).
