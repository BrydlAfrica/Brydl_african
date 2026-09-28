# 06 — Security

## Authentication & sessions

- Werkzeug `generate_password_hash` / `check_password_hash` (scrypt/
  pbkdf2 — never reversible storage).
- Flask-Login with `session_protection="strong"`; **fail-closed
  user_loader** — a DB error during session load treats the request as
  anonymous (never fails open).
- `auth_version` integer: bumping it invalidates ALL of that user's
  sessions (used by password reset/change, admin reset, deactivation).
- Cookies: `HttpOnly`, `SameSite=Lax`, `Secure` when not debugging.
- Login brute force: after `LOGIN_MAX_FAILURES` (5) failures the
  account+IP enters exponential lockout (120 s doubling, cap 900 s);
  failure messages are generic (no user enumeration).

## CSRF & validation

- Flask-WTF CSRF on **every** POST form and every state-changing admin
  POST (even fetch-style ones) — `WTF_CSRF_TIME_TO_LIVE` 12 h.
- All validation is server-side; client attributes are convenience
  only. Forms declare typed fields (DecimalField for money, Length
  validators, etc.).
- Path safety: `os.path.join()` everywhere; user input never reaches
  a path unvalidated.

## Transport & headers (after-request)

`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: same-origin`, conservative `Content-Security-Policy`
(self + inline for the app's own scripts/styles), `Cache-Control: no-store`
on authenticated pages. Production runs behind Render TLS.

## Audit trail (append-only)

`log_event(action, actor, entity_type, entity_id, detail, commit)` —
privileged actions are audited with **counts/metadata only, never
payload data**: logins, verdicts (`review.verdict`, `review.bulk`),
claims (`task.claim`), payments (`payment.generate`, `payment.paid`,
`payment.export_batch`), exports (`project.export`), user management
(`user.create`, `user.toggle_active`, `user.reset_password`,
`user.bank_details_update`). Viewer at `/admin/audit` (filter by actor
and text, paginated). `AuditLog` has no update/delete paths in code.

## Bank-data encryption (PII at rest)

- NUBANs are Fernet ciphertext (`security_crypto.py`; `cryptography`
  pinned). Key = `BANK_ENC_KEY` env, else SHA-256-derived from
  SECRET_KEY (documented tradeoff; dedicated key recommended in prod).
- `bank_account_last4` powers every masked display — the Team page
  **never decrypts**.
- Decryption surface: ONLY the bank-batch CSV generator (in transit),
  plus the annotator's own masked view (last4, no decrypt).
- Legacy plaintext rows (pre-encryption) read through gracefully and
  re-encrypt on next save.
- Audit rows and app logs never contain the number.

## Gold-answer isolation (anti-leak)

`tasks.expected_output` is **never selected or rendered by any
annotator-facing route or template** (a documented rule at the top of
`routes/annotator.py` and enforced by grep tests). It appears only on
admin surfaces (review detail gold-match indicator, exports when
explicitly requested). New annotator endpoints must keep this
invariant — hiding in the UI is not enough; the field is simply not
fetched.

## Schema drift guard

With `BRYDL_AUTO_SCHEMA=0` (production), boot compares the live DB
revision to the migration head; mismatch ⇒ loud RuntimeError naming
`flask db upgrade` as remediation. `flask db` commands bypass the
guard. `flask db check` (suite-tested) confirms models ↔ migrations
parity.

## Input limits (DoS hygiene)

Uploads ≤ `UPLOAD_BATCH_LIMIT` (500) files / import ≤ 2000 rows and
2 MB; QA notes ≤ 2000 chars; pagination everywhere; export streaming
(`yield_per=500`); password-reset rate limits; login lockouts.

## CSV formula-injection guard

Every dataset/bank CSV cell that could start with `=`/`+`/`-`/`@` is
neutralized (prefix-safe encoding) so spreadsheet opening cannot
execute formulas. CSV responses set `Content-Disposition: attachment`
and explicit mimetype.

## Untrusted content rendering (XSS posture, RLHF Phase 3)

Model prompts/responses and task text are attacker-controlled input
by definition. They are rendered only through rendering.py: markdown
conversion followed by an nh3 allowlist sanitizer (tags, attributes,
URL schemes closed by default; span/pre keep only their styling
classes). The system-check suite enforces that no template applies
`|safe` to anything the sanitizer did not produce, and the rendering
suite proves script injection, event handlers and javascript: URLs
never reach any page. Syntax highlighting is Pygments with CSS
classes — no client-side JS runs on task content.

## Repository visibility & token hygiene

The GitHub repo (`brydldata-lab/brydl-dashboard`) is **PRIVATE** —
verified via anonymous fetch 2026-09-18. The blueprint/ folder is
written with ZERO secrets (no hosts, credentials, emails, or URLs);
it describes architecture, not access. Rules that keep it safe:

1. The repo must STAY private. If it ever goes public, first remove
   blueprint/, SECURITY_AUDIT.md, DEPLOY_GUIDE.md,
   POST_DEPLOY_CHECKLIST.md, .env.render.example — and remember git
   HISTORY retains everything ever committed, so the real rule is:
   no secret ever enters the repo at all.
2. 2FA on GitHub and on Render (authenticator app + offline recovery
   codes) — the single highest-value control: one account here
   deploys straight to production.
3. Unique passwords per service (GitHub ≠ Render ≠ email).
4. Collaborators/AI tools get least-privilege, fine-grained, revocable
   tokens — never share the account.

## Known open items (tracked, not hidden)

- RESOLVED 2026-09-20: the stale `region: fra` lines were removed
  from render.yaml. The live service is Oregon; NEVER recreate in
  Frankfurt.
- CLOSED 2026-09-20 (operator-reported): DB credential rotation
  (new default credential + old deleted), GitHub 2FA + Render 2FA
  (recovery codes stored), SMTP verified end-to-end (reset-link email
  received), UptimeRobot monitor, weekly pg_dump routine.
