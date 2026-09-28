# 01 — Product & Rules

## What Brydl is

**Brydl** is a micro-task data-annotation platform for the African
market. Brydl (the operator, a solo CTO in Nigeria) signs up **clients**
who need labeled data; **annotators** (starting at 5, scaling to 50+)
complete tasks from their phones/laptops; Brydl reviews quality and
pays annotators per task. The operator runs everything through one
admin dashboard.

- **Pay band:** $0.05 – $5.00 per task (USD ledger, Naira display/payout).
- **Users:** four roles — `admin` (operator: everything incl. money,
  projects, team, SFT), `qa` (submission review queue + quality),
  `auditor` (consensus arbitration + audit trail) and `annotator`
  (the workspace). QA and auditor users see only their own surface;
  the financial dashboard stays admin-only.
  No multi-tenancy, no client logins (clients receive exports; a token
  link portal is a future tier), no public API.
- **Growth target:** 5 → 50+ concurrent annotators without degradation.
- **Environment reality:** the operator works from Nigeria with
  unreliable connectivity → the app is **offline-first** (PWA, self-
  hosted assets, no CDN dependencies at runtime) and must stay usable
  on flaky networks.

## The non-negotiable rules (do not deviate)

1. **Stack:** Flask 3.x on Python 3.10+, Jinja2 templates,
   SQLAlchemy ORM only (no raw SQL), Flask-Migrate/Alembic migrations,
   Flask-Login + Werkzeug password hashing, Flask-WTF + CSRF on every
   form, Bootstrap 5 + Bootstrap Icons **self-hosted** in
   `static/vendor/` (no npm, no CDN), Gunicorn on Render.
2. **Databases:** SQLite for local dev and all tests; PostgreSQL in
   production. `DATABASE_URL` selects the backend. In production the
   env var `BRYDL_AUTO_SCHEMA=0` makes Alembic migrations the
   authority (no `create_all` at boot); local dev may auto-create.
3. **Architecture:** application factory (`app.py`), blueprints
   `auth`, `admin`, `annotator`, `public`; single `models.py`;
   helpers `config.py`, `extensions.py`, `audit.py`, `backup.py`,
   `mailer.py`, `notifications.py`, `security_crypto.py`, `seed.py`,
   `seed_admin.py`.
4. **Money:** `Decimal` end-to-end. `Numeric(10,2)` columns, a
   `models.money()` normalizer at every boundary, `DecimalField`
   forms. Ledger in USD; payouts converted to Naira via the
   `USD_TO_NGN_RATE` knob; Naira display only.
5. **Security posture:** role-based access on every protected route,
   server-side validation (never trust the browser), flash messages,
   try/except around DB operations with honest user-facing errors,
   audit every privileged mutation, UTC everywhere.
6. **Scalability rules:** DB indexes on queried columns; every list
   paginated (20/page default); zero N+1 queries (joinload or explicit
   selects); 50+ concurrent annotators must pass the stress gate;
   `os.path.join()` for all paths; **zero hardcoded values** — every
   tunable is a config knob.
7. **Code style:** PEP 8 + docstrings everywhere (flake8 clean is an
   acceptance gate); production-complete code only — no TODOs, no
   placeholders, no stubs.
8. **Tests before/with every claim:** no feature is "done" without a
   test proving it. The full battery must stay green (20 suites).
9. **Deviation clause:** deviations from the above are permitted when
   "it will work best and securely" demands it, but require a written
   explanation (see `11_BUILD_HISTORY.md` for the ones made).
10. **Explain in chat, document in repo:** operational walkthroughs go
    step-by-step to the operator in chat; durable knowledge lives in
    the repo (README, DEPLOY_GUIDE, SECURITY_AUDIT, this blueprint).

- **Task types:** classification, translation, NER, rlhf (1-5
  rating), **preference (RLHF/DPO: prompt + Response A/B side by
  side, chosen + required written justification, deterministic
  per-annotator position randomization)**, audio_transcription,
  image_labeling - all data-driven via `models.TASK_TYPES`.

## Quality philosophy (how decisions get made)

- Human review of every submission is *correct* at small scale; scale
  via **earned trust** later, not blind score thresholds.
- Feedback loops must **terminate**: a rejected task cycles at most
  `TASK_MAX_REJECTIONS` (3) times before escalating out of the loop.
- The highest-value PII (bank account numbers) is **encrypted at
  rest**; decrypted only at the moment of payout-file generation.
- Annotators are **pseudonymous in datasets** (numeric ids only).
- Gold answers (`tasks.expected_output`) are the QA instrument and are
  **never selectable or renderable by any annotator-facing route**.
