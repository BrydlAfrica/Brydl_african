# Build Report — User Management & First-Admin CLI

- **Build:** 15 · **Date:** 2026-09-10 (UTC)
- **Status:** ✅ Complete — 10/10 test suites, 36/36 verification checks, PEP 8 clean repo-wide
- **Scope:** Admin UI to manage accounts end-to-end + a one-command first-admin bootstrap, closing the last "hand-edit the database" gap before real annotators are onboarded.

---

## 1. Executive summary

Until this build, creating an annotator required inserting a database row by
hand — impossible to do safely from a phone, and the documented production
workaround was a raw Python snippet against the live database. This build
replaces all of it with two things:

1. **The Team page** (`/admin/users`) — create accounts, deactivate leavers,
   and reset passwords, all from the browser, all audited.
2. **`flask --app app seed-admin`** — a guarded CLI that creates the very
   first admin and refuses to do anything else.

Design principle throughout: **no schema changes, no new dependencies, no
email infrastructure.** The build reuses the existing session-revocation,
audit-trail, CSRF and hashing machinery that earlier security builds put in
place, so the new feature inherits tested protections rather than new risk.

---

## 2. What was built

### 2.1 Team page — account lifecycle from the browser

| Capability | Behaviour |
| --- | --- |
| **Create account** | Name + email + role (annotator/admin). A strong temporary password (~12 chars, ~70 bits entropy, `secrets.token_urlsafe`) is generated **server-side** and shown **once** in a flash message. It is never stored in plain text, never logged, never audited. |
| **Deactivate** | Ends the person's live sessions **immediately** (the user loader rejects inactive accounts mid-request — the mechanism proven in the security audit). Hides them from annotator pickers. History and payouts are kept. |
| **Reactivate** | Restores sign-in. The old password still works; if it was compromised, reset it first. |
| **Reset password** | New one-off password, same shown-once discipline. Works on inactive accounts too (pre-stage a password before re-onboarding). The old password stops working the moment it is reset. |
| **Workload & activity** | Per-annotator open-task counts; a **last-login column derived from the audit trail** (`auth.login.success`) instead of a new column — zero schema change. |
| **List hygiene** | Sorted active-first, then role, then name; **paginated at 20/page** per the standing scalability rule (3 queries per page regardless of team size: accounts + grouped open-tasks + grouped last-logins). |

**Safety guards (enforced server-side, not just in the UI):**

- You cannot deactivate your own account.
- The **last active admin cannot be deactivated**.
- Duplicate emails are refused case-insensitively (`TUNDE@…` = `tunde@…`).
- Email is validated server-side (WTForms `Email`) and normalised to lowercase.
- Every state-changing route is **POST-only with CSRF validation**.

**Audit trail (5 new actions):** `user.create`, `user.deactivate`,
`user.reactivate`, `user.password_reset` (actor-attributed) and
`system.seed_admin` (CLI). Passwords never appear in any audit record.

### 2.2 `flask seed-admin` — first-admin bootstrap

```bash
flask --app app seed-admin --email you@brydl.ng            # hidden prompt
SEED_ADMIN_PASSWORD=… flask --app app seed-admin --email you@brydl.ng   # scripted deploys
```

| Guard | Result |
| --- | --- |
| Any active admin already exists | **Refuses** — further admins are created from the Team page where actions are actor-attributed. |
| Password < 8 or > 128 chars | Refuses. |
| Malformed or duplicate email | Refuses; email lower-cased on the way in. |
| Password hygiene | Accepted from hidden prompt, `--password`, or `SEED_ADMIN_PASSWORD` (non-interactive Render deploys); warned against in help text. |

This deleted the README's "temporary raw-Python snippet against production"
step — deploy Step 3 is now two commands.

---

## 3. Files changed

| File | Change |
| --- | --- |
| `templates/admin/users.html` | **New** — team page (create form, roster table, guards, pager); no inline JS handlers (CSP-safe). |
| `seed_admin.py` | **New** — the CLI command with all guards + audit. |
| `tests/test_user_mgmt.py` | **New** — 35-check suite (§4); auto-runs in the battery. |
| `routes/admin.py` | +4 routes (`GET /users`, `POST /users/create`, `POST /users/<id>/toggle-active`, `POST /users/<id>/reset-password`), create-form, password generator, pagination. |
| `app.py` | Registers `seed_admin_command` beside the backup CLI. |
| `templates/base.html` | "Team" nav entry (admin navbar only). |
| `README.md` | Deploy Step 3 rewritten to the CLI; runbook password-reset row now points at the Team page; tree/counts updated; roadmap trimmed of shipped items. |
| `SECURITY_AUDIT.md` | New section documenting the team build's controls. |

No changes to `models.py` (schema untouched), `config.py`, or any other route module.

---

## 4. Verification

### 4.1 New suite — `tests/test_user_mgmt.py` (35 checks, all pass)

| Section | Proves |
| --- | --- |
| seed-admin CLI (7) | Refuses short password, malformed email, second admin, duplicate email; creates first admin (email case-normalised); `system.seed_admin` audited; exactly one active admin. The CLI runs as a **real subprocess** (`python -m flask …`), not a mocked call. |
| RBAC (4) | Anonymous → 302 to sign-in (page + create); seeded admin signs in; annotator → **403** on both team routes. |
| Create account (8) | One-off password flashed and **actually signs in**; DB row is an active annotator; `user.create` audited; duplicate + malformed emails refused; page lists accounts with status. |
| Deactivate / reactivate (7) | Deactivation **revokes a live session mid-flight**; `is_active` flips in DB; audited; reactivation restores sign-in; self-deactivation refused. |
| Reset password (4) | New one-off works, **old password is dead**, audited. |
| Pagination (3) | 22 accounts → "Showing 20 of 22" + pager; page 2 holds the remainder; out-of-range page handled. |
| Hygiene (2) | No inline event handlers in the template; Team nav link present. |

### 4.2 Whole-system regression (after the build)

| Gate | Result |
| --- | --- |
| 10-suite battery (`test_annotator/audit/deploy/hardening/payments/review/scalability/system_check/tasks/user_mgmt`) | **10/10, exit code 0 on every suite** |
| System check (auto-absorbed the 3 new POST routes) | CSRF matrix + role-decorator crawl stay green |
| Full verification report (`tests/full_report.py`) | **36/36** — security 16/16, validation 10/10, features 6/6, user 4/4 |
| Performance (400-req concurrency barrier, 16 threads) | 0 errors, ~140–200 req/s in-process, p95 ≈ 135–183 ms |
| Race safety (12 concurrent identical submitters) | Exactly **1** submission row |
| PEP 8 (`flake8`, whole repo, 79-col) | Clean |

---

## 5. Design decisions & rationale

| Decision | Why |
| --- | --- |
| **Server-generated passwords** (not admin-chosen) | Admins habitually pick weak, reused passwords; a `secrets`-based one-off cannot be weak, and "shown once" makes casual reuse visible. |
| **No email delivery** | No SMTP dependency, no credentials to leak, nothing to break on a free tier. Passwords move over whatever private channel the admin already uses (WhatsApp/SMS in person). Email invites are a roadmap item, not a dependency. |
| **Deactivate, never delete** | Payment history and audit integrity demand the person's row survive; deactivation already revokes sessions via the audited user-loader path, so "fire someone" is instant and testable. |
| **Last login from the audit trail** | The information already existed; deriving it avoids a schema change (and therefore avoids needing migrations for this build). |
| **Second-admin refusal in the CLI** | The CLI runs un-attributed (`system.seed_admin`); the Team page attributes actions to a signed-in admin. Forcing all subsequent account work through the UI keeps a clean responsibility chain. |
| **Validation split** | WTForms handles the create form (field errors re-render inline); the POST-only toggle/reset routes take raw CSRF-validated params — same pattern as the rest of the app. |

---

## 6. Known limitations / next steps

1. **Password self-service** — users still cannot change their own password or trigger a reset; admins do it. On the roadmap (needs email or admin-mediation policy).
2. **No invite emails** — one-off passwords are hand-delivered by design (§5).
3. **`models.py` `get_total_earnings`** still computes on floats at one internal boundary (display-only; DB and payouts are Decimal) — queued tidy-up.
4. **Deploy cutover** — the feature is verified on SQLite; the README's PostgreSQL path (psycopg2 → `flask seed-admin` against the Render DB) is the next build.

---

## 7. How to use it (operator cheat-sheet)

```bash
# First admin, right after deploying:
SEED_ADMIN_PASSWORD='a-long-unique-passphrase' flask --app app seed-admin --email you@brydl.ng

# Then, in the app (admin → Team):
#   Create account  → copy the one-off password → hand it to the person
#   Reset password  → same flow, for lost/compromised passwords
#   Deactivate      → leaver's sessions die instantly, history kept
```
