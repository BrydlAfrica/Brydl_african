# RedLab (v14) — Dynamic Red-Teaming as a Service

**Status:** Phase 1 (ASGI host + healthcheck) complete · flag OFF in prod  
**Author:** Brydl Eng · **Last updated:** 2026-10-08 (Phase 1 merged)

---

## 1. Product goal

RedLab is Brydl's **second product line**, sold alongside data labeling:
*dynamic, African-language AI red-teaming*. We leverage the same 50+
annotator pool that does labeling (Hausa / Yoruba / Swahili / French) to
adversarially probe client LLMs for safety violations, prompt-injection
vulnerabilities, jailbreaks, and canary leaks — with per-breach bounties
paid to the annotator who finds them, QA-confirmed, and exported in
OpenAI / ShareGPT format.

### Non-goals

- Not a standalone deploy. Single Render free-tier web service, single
  Postgres, single domain. No second service, no second domain, no
  separate auth.
- Not a replacement for Flask. Flask owns auth, users, projects, tasks,
  QA, and all Decimal-money payouts for the life of v14.
- Not exposing the RedLab API publicly until Phase 9.
  `REDLAB_ENABLED=false` is the default through Phases 1–8.

## 2. Architecture (LOCKED per Phase 0)

```
                            ┌────────────────────────────────┐
            :443 (Render)   │  gunicorn -k UvicornWorker ×2  │
                            │  asgi:app (FastAPI root)       │
                            └────────────┬───────────────────┘
                                         │ mounts
                     ┌───────────────────┴───────────────────┐
                     │                                       │
               /redlab/*                                    /*
                     │                                       │
         ┌───────────▼───────────┐            ┌──────────────▼──────────────┐
         │ redlab.app (FastAPI)  │            │ a2wsgi.WSGIMiddleware       │
         │  /healthz  (always)   │            │  wrapping app:create_app()  │
         │  /v1/*     (gated)    │            │  /login /annotator /admin…  │
         │  - JSON 500 boundary  │            │  (unchanged Flask codebase) │
         │  - X-Request-ID       │            └─────────────────────────────┘
         └───────────────────────┘
```

- **Single repo / single deploy / single DB.**
- **FastAPI wraps Flask** via Starlette `WSGIMiddleware` from `a2wsgi`.
  Flask is mounted at `/`, RedLab at `/redlab`.
- **RedLab tables in Postgres schema `redlab`** — never touch the
  `public.*` tables Flask already owns (except additive columns in
  Phase 2 on `submissions` and `projects`, plus the new
  `dynamic_red_team` task type).
- **Client API keys Fernet-encrypted at rest** (`redlab/crypto.py`, key
  from `REDLAB_FERNET_KEY`). Never plaintext; never logged.
- **Feature flag** `REDLAB_ENABLED=true` is required for any `/redlab/v1/*`
  traffic. Healthz at `/redlab/healthz` is always 200 (so we can switch
  Render's health-check over before flag-on day).
- **Strict CSP preserved** — no inline JS anywhere. RedLab UI code lives
  in `static/js/redteam_chat.js` (Phase 6).
- **Import boundary (enforced in Phase 9 by `import-linter`):**
  - `redlab.*` MUST NOT import Flask routes, templates, or Flask
    business logic.
  - Flask MUST NOT import from `redlab.*` except via `redlab.integration`
    (the one bridge module that posts session-concluded / breach-flagged
    events back onto Flask's submission + QA tables).
- **All existing test suites green at every phase.**

### Worker/thread sizing (free-tier tuned)

| Resource | v13 (Flask-only) | v14 (ASGI) |
|----------|------------------|------------|
| gunicorn workers | implicit 1 (gunicorn default) | 2 |
| Worker class | sync | `uvicorn.workers.UvicornWorker` |
| Threads | `--threads 4` (sync Gunicorn) | AnyIO to-thread pool = **60 tokens** (set in `asgi.py` startup) |
| Health check  | `/login` | `/redlab/healthz` |

Projected idle RAM on free tier (~512 MB): 2 workers × ~180 MB ≈ 360 MB,
leaving ~150 MB for httpx + SQLAlchemy under annotator load.

## 3. Data model (Phase 2 — to be implemented)

Four new tables in Postgres schema `redlab`:

- **Target** — client LLM endpoint (URL, Fernet-encrypted API key,
  model name, owner_id, active).
- **Session** — one annotator's red-team conversation with a target
  (target_id, annotator_id, project_id, started_at, concluded_at,
  turn_count, breach_count, final_state ∈ `active|concluded|rejected`,
  client_turn_idempotency_key).
- **Message** — one chat turn (session_id, role ∈ `user|assistant|system`,
  body text, connector_error nullable, created_at, flagged_breach bool,
  qa_override nullable).
- **ProcessedEvent** — dedup table for in-process events (event_id UUID
  primary key, event_type, created_at) so duplicate deliveries are
  no-ops.

Additive columns on core tables (schema `public`):

- `submissions.redlab_session_id` UNIQUE NULLABLE — links a submission
  to its RedLab session on conclude.
- `submissions.redteam_state` DEFAULT 'none' — annotator workflow state.
- `submissions.redteam_breach_count` DEFAULT 0.
- `projects.redteam_base_pay` Numeric(10,2) DEFAULT 0.
- `projects.redteam_breach_bounty` Numeric(10,2) DEFAULT 0.
- `projects.redteam_session_quota` INT DEFAULT 0 (0 = unlimited).

## 4. API surface (Phases 1 → 8)

| Phase | Method | Path | Purpose |
|-------|--------|------|---------|
| 1 | GET | `/redlab/healthz` | liveness probe (always on) |
| 1 | GET/POST/… | `/redlab/v1/*` | 404 `{error:redlab_disabled}` while flag OFF |
| 4 | POST | `/redlab/v1/sessions/{sid}/turn` | annotator chat turn (idempotent by `client_turn_id`) |
| 5 | POST | `/redlab/v1/messages/{mid}/flag` | flag/unflag a breach |
| 5 | POST | `/redlab/v1/sessions/{sid}/conclude` | close session (min 3 turns, idempotent) |
| 8 | GET | `/api/v1/export?task_type=dynamic_red_team&format=openai` | JSONL export (approved only) |
| 8 | GET | `/api/v1/export?…&format=sharegpt` | ShareGPT format |

All RedLab JSON errors follow `{ "error": "<code>", ... }` with
`X-Request-ID` header (set by the JSON-500 boundary middleware).

## 5. Phase rollout (LOCKED order, explicit go after each)

- [x] **Phase 1 — ASGI host + healthcheck** (this commit)
- [ ] Phase 2 — Migration #18, redlab.models, redlab.crypto
- [ ] Phase 3 — Async connector (httpx), SSRF defense, mock model
- [ ] Phase 4 — Turn API + session-cookie/CSRF auth
- [ ] Phase 5 — Breach flagging, event dispatcher, integration bridge
- [ ] Phase 6 — Annotator chat UI + claim hook
- [ ] Phase 7 — QA console + Decimal bounty ledger
- [ ] Phase 8 — Exporters (OpenAI/ShareGPT), quotas, attack-prompt carousel
- [ ] Phase 9 — Lint contracts, hardening, flag rehearsal → tag v14.0

## 6. Rollout plan (production)

1. Admin takes a **`pg_dump` from laptop** (Render free tier has no
   Shell / no Backups tab) — see `RENDER_BACKUP_GUIDE.md`.
2. Push v13.1 → Render (Hausa bar + logo, no ASGI change) — still
   Flask-only, old Procfile.
3. Merge v14-redlab → main with `REDLAB_ENABLED` unset (default OFF).
   Render deploys gunicorn+uvicornworker. Health check switches to
   `/redlab/healthz` (always 200). Flask routes continue to serve;
   `/redlab/v1/*` returns JSON 404 `redlab_disabled`.
4. Smoke-test in production against `/redlab/healthz`, `/login`,
   `/annotator/`, `/admin/`.
5. When ready, set `REDLAB_ENABLED=true` + `REDLAB_FERNET_KEY=<key>` in
   Render dashboard, redeploy. Phases 2–8 ship incrementally on top.

## 7. Rollback

If ASGI causes an incident:

- **10-second rollback:** point Render start command back to
  `gunicorn --threads 4 "app:create_app()"` and healthCheckPath back to
  `/login`. Flask's `app:create_app()` module is untouched by v14 — it
  is still a fully working Flask app. No data migration to reverse
  (Phase 2 migration is additive; downgrade script drops the `redlab`
  schema cleanly).

## 8. Key files

| File | Purpose |
|------|---------|
| `asgi.py` | Root ASGI composer — gunicorn entry point |
| `redlab/__init__.py` | Package docstring + import contract |
| `redlab/flags.py` | `REDLAB_ENABLED` flag helper |
| `redlab/app.py` | FastAPI sub-app (/healthz, /v1 gate, JSON-500 middleware) |
| `render.yaml` | Render Blueprint — workers=2, UvicornWorker, healthz=/redlab/healthz |
| `Procfile` | Local/Heroku-style start command (matches render.yaml) |
| `tests/test_phase1_asgi.py` | 31 ASGI assertions (flag on/off, routes, error boundary, threadpool, deps) |
| `tests/test_deploy.py` | Updated deploy-file pins for ASGI |
| `tests/test_scalability.py` | Updated threadpool pin |
