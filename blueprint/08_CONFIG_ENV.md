# 08 — Config & environment variables

`config.py` owns every knob. Zero hardcoded values elsewhere. Booleans
are `"1" == value`; ints via `_env_int`; decimals via `_env_decimal`.

## Core

| Var | Default | Behavior |
|---|---|---|
| `DATABASE_URL` | SQLite `instance/brydl.db` | `postgresql://…` selects Postgres (Render injects it); internal vs external URL both accepted |
| `SECRET_KEY` | instance-file fallback chain | env first; else a generated key persisted at `instance/secret_key` (0600) so dev sessions survive restarts; **must be env-set in production** |
| `BRYDL_AUTO_SCHEMA` | `1` | `0` in production: migrations authoritative, boot guard active, no `create_all` |
| `FLASK_ENV` / debug | development | production sets `production`; drives cookie Secure |
| `PYTHON_VERSION` (runtime.txt) | 3.13 | Render runtime pin |

## Pagination / limits

| Var | Default |
|---|---|
| `PAGINATION_PER_PAGE` | 20 |
| `UPLOAD_BATCH_LIMIT` | 500 |
| `IMPORT_MAX_ROWS` | 2000 |
| `IMPORT_MAX_BYTES` | 2 000 000 |
| `MAX_QA_NOTES_CHARS` | 2000 |
| `EXPORT_YIELD_PER` | 500 |
| `MEDIA_MAX_BYTES` | 8 388 608 (8 MB) — per-file media upload cap (Phase 8) |

## Auth / abuse

| Var | Default |
|---|---|
| `LOGIN_MAX_FAILURES` | 5 |
| `LOGIN_LOCKOUT_BASE_SECONDS` | 120 |
| `LOGIN_LOCKOUT_MAX_SECONDS` | 900 |
| `FORGOT_MAX_PER_WINDOW` | 3 |
| `FORGOT_WINDOW_SECONDS` | 900 |
| `RESET_TOKEN_MAX_AGE_SECONDS` | 3600 |
| `BRYDL_TERMS_VERSION` | "1.0" (bump to re-prompt all annotators) |

## Operations (Tier 1 + system fixes)

| Var | Default | Behavior |
|---|---|---|
| `CLAIM_DAILY_CAP` | 20 | self-claims per annotator per UTC day |
| `TASK_MAX_REJECTIONS` | 3 | revision-loop cap → auto-return + escalation |
| `PREFERENCE_MIN_JUSTIFICATION` | 40 | min chars for RLHF preference justifications |
| `MARKDOWN_RENDER_MAX_CHARS` | 20000 | beyond this, content renders as escaped plain text |
| `SFT_MAX_TEXT_CHARS` | 20000 | per-field cap on SFT prompt/completion/system text |
| `SFT_EXPORT_MAX_ROWS` | 5000 | hard cap on one SFT export batch (scope smaller instead) |
| `MAX_RUBRIC_DIMENSIONS` | 6 | max rubric dimensions per preference project |
| `MAX_RUBRIC_DIM_NAME` | 40 | max characters per dimension name |
| `RUBRIC_SCALE_MIN` | 1 | inclusive lower slider bound |
| `RUBRIC_SCALE_MAX` | 5 | inclusive upper slider bound |
| `PAYSTACK_SECRET_KEY` | (unset) | sk_* secret; unset = Paystack disabled, CSV payouts only |
| `PAYSTACK_BASE_URL` | https://api.paystack.co | API root (test gateway overridable) |
| `PAYSTACK_TIMEOUT_SECONDS` | 15 | per-call HTTP timeout |
| `SITE_URL` | "" | public base URL, no trailing slash; makes notification emails clickable |
| `BANK_ENC_KEY` | derived from SECRET_KEY | dedicated Fernet key for bank numbers (recommended in prod) |
| `USD_TO_NGN_RATE` | 1600 | Decimal FX for display/payout CSV |

## Email (SMTP) — optional, no-op safe when unset

| Var | Default |
|---|---|
| `SMTP_HOST` | "" (unset ⇒ emails disabled; reset links go to app log in dev) |
| `SMTP_PORT` | 587 |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | "" (Gmail: 16-char app password) |
| `MAIL_FROM` | "Brydl <no-reply@brydl.local>" |
| `SMTP_STARTTLS` | 1 |

`Config` mirrors every module-level knob as a class attribute (tests
and `current_app.config` read one way).

## Engine options (config.py)

- SQLite: `creator = _sqlite_connect_with_retry(path)` — a factory
  bound to **that engine's file path** (tests/subprocesses open other
  DBs); 4 attempts, sleep 0.01·(attempt+1), `sqlite3.connect(path,
  timeout=15, check_same_thread=False)`. Busy timeout lives INSIDE the
  creator (connect_args are ignored when a creator is set).
- PostgreSQL: `connect_args={"connect_timeout": 10}`.
- Both: `pool_pre_ping=True`; PG `pool_size=10, max_overflow=20`.

## Secrets generation (copy-paste)

```
Fernet key:  python3 -c "from cryptography.fernet import Fernet; \
             print(Fernet.generate_key().decode())"
Flask key:   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```
