# Brydl Dashboard — Full System Verification Report

- **Generated:** 2026-09-13 05:34:44 UTC
- **Environment:** Python 3.13.14 · Linux 6.1.158+
- **Total checks:** 36 · **Passed:** 36 · **Failed:** 0
- **Wall time:** 6.4s (checks 5.8s)
- **Verdict:** ✅ **PASS**

## Summary by area

| Area | Checks | Passed | Failed | Section time |
| --- | --- | --- | --- | --- |
| SECURITY | 16 | 16 | 0 | 0.8s |
| VALIDATION | 10 | 10 | 0 | 0.1s |
| FEATURES | 6 | 6 | 0 | 0.2s |
| USER | 4 | 4 | 0 | 0.2s |

## 🔐 SECURITY

| # | Result | Condition & detail |
| --- | --- | --- |
| 1 | ✅ | Anonymous is redirected to sign-in everywhere — 302 -> /login on protected routes |
| 2 | ✅ | Role wall: cross-role access is 403 (not 404/200) — admin↔annotator isolation |
| 3 | ✅ | Foreign task access returns 404 (anti-probing) — annotator A opening B's task #2 |
| 4 | ✅ | CSRF: missing/wrong token refused on every POST — login, project, upload, assign, review, payments6 endpoints probed |
| 5 | ✅ | Security headers on every response — CSP, X-Frame-Options, nosniff, Referrer-Policy, X-Request-ID |
| 6 | ✅ | Session cookie flags: HttpOnly + SameSite — browser scripts cannot read the session cookie |
| 7 | ✅ | Session fixation: identity rotates on sign-in — pre-login and post-login session values differ |
| 8 | ✅ | Deactivated user's live session is revoked — user_loader rejects inactive accounts mid-session |
| 9 | ✅ | Open-redirect protection on ?next= — external targets ignored after sign-in |
| 10 | ✅ | Brute-force lockout after 5 failures — escalating lockout answers 429 |
| 11 | ✅ | SQL injection: login bypass impossible — 'OR 1=1 payloads rejected by the ORM + validators |
| 12 | ✅ | SQL injection: filter params cannot touch data — status/project/annotator params are validated against vocab/DB |
| 13 | ✅ | Stored XSS: project name renders escaped — <script> payload never reaches the DOM raw |
| 14 | ✅ | Stored XSS: QA notes render escaped on task page — <img onerror> payload neutralised |
| 15 | ✅ | ID probing on dynamic routes: 404, never 500 — review/payment/project pages for absurd ids |
| 16 | ✅ | Gold answers never leak to annotators — expected_output absent from annotator routes/pages |

## ✅ VALIDATION

| # | Result | Condition & detail |
| --- | --- | --- |
| 1 | ✅ | Login: malformed email rejected server-side |
| 2 | ✅ | Project: pay_per_task 0 / negative / junk rejected |
| 3 | ✅ | Project: instructions capped at 10,000 chars |
| 4 | ✅ | Upload: empty data / oversized line rejected — atomic: nothing saved for a bad batch |
| 5 | ✅ | Upload: >500 batch rejected |
| 6 | ✅ | Assign: empty selection / bad annotator refused |
| 7 | ✅ | Review: score bounds + notes cap enforced |
| 8 | ✅ | Annotator: empty/oversized/illegal answers refused |
| 9 | ✅ | Payments: unknown method refused |
| 10 | ✅ | Query params: junk pages/filters never 500 |

## ⚙️ FEATURES

| # | Result | Condition & detail |
| --- | --- | --- |
| 1 | ✅ | Project lifecycle: create → edit → pause → resume — every step audited |
| 2 | ✅ | Upload → assign → unassign → reassign round-trip — (tasks 4-6) |
| 3 | ✅ | Annotate → review approve: score/notes/task state |
| 4 | ✅ | Reject-with-revision loop: banner → redo → approve |
| 5 | ✅ | Payments: generate → mark-paid → ledger consistent |
| 6 | ✅ | Filters + pagination return consistent data |

## 👤 USER

| # | Result | Condition & detail |
| --- | --- | --- |
| 1 | ✅ | Admin dashboard statistics match the database |
| 2 | ✅ | Annotator dashboard reflects their own queue/earnings |
| 3 | ✅ | Data isolation between annotators |
| 4 | ✅ | Logout ends the session (POST-only) |

## ⚡ Performance analysis

Latency measured in-process (Flask test client → pure application time, no network), 30 sequential requests per page, after seeding ~620 tasks / 230 submissions.

### Page latency & weight

| Page | p50 (ms) | p95 (ms) | p99 (ms) | mean (ms) | max (ms) | avg size (KB) | avg SQL queries |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `/login` | 0.9 | 1.1 | 1.2 | 0.9 | 1.2 | 6.3 | 0.0 |
| `/admin/` | 2.7 | 3.2 | 3.2 | 2.7 | 3.2 | 14.3 | 7.0 |
| `/admin/projects` | 2.3 | 2.9 | 2.9 | 2.4 | 2.9 | 12.7 | 4.0 |
| `/admin/tasks` | 4.1 | 4.8 | 5.6 | 4.2 | 5.6 | 28.3 | 7.0 |
| `/admin/review` | 3.5 | 4.2 | 4.5 | 3.6 | 4.5 | 35.5 | 7.0 |
| `/admin/payments` | 3.6 | 4.4 | 4.4 | 3.6 | 4.4 | 14.2 | 9.0 |
| `/admin/audit` | 2.5 | 3.0 | 3.1 | 2.5 | 3.1 | 22.6 | 4.0 |
| `/annotator/` | 3.4 | 3.9 | 4.4 | 3.5 | 4.4 | 12.2 | 9.0 |
| `/annotator/history` | 2.4 | 2.8 | 2.8 | 2.4 | 2.8 | 19.6 | 4.0 |

Slowest page at p95: `/admin/tasks` (4.8 ms). All list views are paginated and index-backed; query counts stay flat as rows grow (N+1-free).

### Throughput (concurrency stress)

- **16 threads × 25 mixed requests = 400 requests** in 1.67s → **240 req/s** (in-process).
- p50 61.1 ms · p95 112.6 ms · p99 137.3 ms under concurrency.
- Errors under load: **0** (none)

### Race safety (double-submit under contention)

- 12 concurrent identical submissions on task #828: **1 submission row(s) created**, final task status `submitted` → ✅ exactly-once

### Bulk operations

- 500-task atomic upload: 34 ms (14898 tasks/s) — ✅
- Bulk approve of 30 submissions in one request: 9 ms — ✅

### Database at report time

| Table | Rows |
| --- | --- |
| users | 4 |
| projects | 3 |
| tasks | 1,328 |
| submissions | 35 |
| payments | 2 |
| audit_logs | 61 |
| database size | 4.0 KB |
| integrity_check | ok |

## Notes & limitations

- Latency/throughput are **in-process** figures: they measure application + database efficiency without network overhead. On Render add TLS/proxy latency (~10–50 ms) and free-tier CPU contention.
- The brute-force lockout test intentionally burns one lockout slot for a throwaway address (per email+IP key).
- Password change/user-management flows are roadmap items (see README §8) and therefore not covered here.
