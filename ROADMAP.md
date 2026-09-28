# Brydl Dashboard — Roadmap to a full labeling-company platform

Status as of 2026-09-13: **v1 is deploy-ready** (18/18 test suites, 6
migrations, admin+annotator workflows complete: projects → tasks (paste
& CSV import) → assignment → annotation → QA review → payout runs with
bank CSV → audit trail). This document is the honest gap list between
"working dashboard" and "full AI data labeling startup platform",
prioritized for a solo CTO.

---

## Tier 0 — do these TODAY, before/with the deploy (hours, not days)

| # | Item | Why now | Effort |
|---|------|---------|--------|
| 0.1 | Edit `templates/annotator/terms.html` to YOUR real contract | It is currently a solid draft; it is a legal document — make it yours before annotators accept it | 1 h reading/editing |
| 0.2 | Set SMTP env vars (Gmail app password is fine to start) | Password reset by email instead of reading the log; annotators can recover accounts without you | 15 min |
| 0.3 | `flask --app app seed-admin` with a strong password; never run seed.py in prod | The guard refuses it anyway — but create the real admin | 5 min |
| 0.4 | External uptime monitor on `/login` (UptimeRobot, free) | Render sleeping/free-tier hiccups surface before annotators message you | 10 min |
| 0.5 | Weekly `pg_dump` reminder (calendar) + download off-machine | Render free DB expires ~30 days; DEPLOY_GUIDE §3.2–3.3 has the commands | 10 min |
| 0.6 | Set `USD_TO_NGN_RATE` env var to today's rate | Display-only rate defaults to 1600; it is a knob precisely so you can update it without a deploy | 2 min |

## Tier 1 — removes the operational bottlenecks at 5→50 annotators
**SHIPPED 2026-09-17** (self-claim + cap, notifications, deadlines,
quality dashboard; 19/19 suites green)

1. **Annotator self-claim from a pending pool** (≈1–2 days)
   Admin assignment is the single biggest bottleneck: every new batch
   needs manual assign clicks. Add "Claim next task" on the annotator
   dashboard: race-safe conditional UPDATE on pending tasks of an
   active project (the exact pattern the submit guard already uses),
   per-annotator daily claim cap (config), claim → auto-assign →
   normal flow. Admin assignment stays for targeted work.

2. **In-app notifications + email on events** (≈1–2 days)
   Today annotators discover rejections/payments by logging in. Add a
   `notifications` table (user_id, kind, payload, read_at) + a bell
   badge; email (the SMTP path exists) on task assigned, task
   rejected, payout marked paid. Reuse the existing mailer; events
   already flow through the audit layer.

3. **Project deadlines + annotator-facing due dates** (≈0.5 day)
   `projects.deadline` (Date, nullable), shown on the dashboard queue
   and in claim/assign flows. Terms already promise deadlines — the
   schema should back that.

4. **Annotator quality dashboard (admin)** (≈1 day)
   Per annotator: approval rate, average QA score, gold-hit rate,
   rejection/revision counts, last-30-day trend (all derivable from
   submissions + gold answers already stored). This is how you decide
   who gets more work at 50+ people. Pairs with a "top-up work to
   best performers" habit.

## Tier 2 — money & trust (the startup layer)

5. **Paystack integration** (≈3–5 days)
   - In: fund a company wallet via Paystack Inline/checkout ( ₦
     native, cards + transfer + USSD).
   - Out: Paystack Transfers API for payouts (recipient = the NUBAN
     the annotator already saved; create recipient once, then bulk
     transfer) — replaces the manual bank-CSV run as volume grows.
   - Keep the manual CSV path as fallback forever; Paystack webhook +
     verify → mark paid with audit rows. Start in test mode.

6. **Client portal (read-only, login-optional via signed links or
   client accounts)** (≈3–4 days)
   A labeling startup's clients want: progress %, reviewed sample
   download, invoice/summary per project. Even a single
   `/client/<token>/project/<id>` page per project moves you from
   "internal tool" to "company platform". Token-based first (no new
   auth system), rotate/revoke tokens in admin.

7. **NDPR privacy page + data export/delete requests** (≈1 day)
   You hold annotator PII (bank details, phone) and client data. A
   public `/privacy` page + an admin "export account data" action
   covers the Nigerian Data Protection baseline and enterprise client
   questionnaires.

## Tier 3 — the platform features (differentiators, bigger lifts)

8. **Media tasks: image/audio annotation** (≈2–3 weeks)
   The flagship gap versus Label Studio et al. V1 scope: image
   classification (upload images with the task, S3/Cloudflare R2 or
   even signed Render disk URLs at small scale), bounding boxes later.
   New Task type + media column + annotator canvas; keep text tasks
   untouched.

9. **Consensus / inter-annotator agreement** (≈1 week)
   `tasks.required_annotations` (default 1): when >1, the task goes to
   N annotators; agreement computed on approval (exact match for
   classification); disagreement → arbitration queue for the admin.
   Combined with gold checks this becomes a client-grade quality
   story ("95% agreement, gold-verified").

10. **Self-hosted Bootstrap/Icons** (≈2 h) — drop the CDN dependency
    (offline-first reliability + Nigeria latency); download the pinned
    5.3.8/1.13.1 files into static/vendor.

11. **Live FX** (≈2 h) — nightly scheduled fetch of USD→NGN into the
    existing `USD_TO_NGN_RATE` knob (Render cron or a manual CLI
    command); display-only, money math already Decimal-safe.

12. **Avatar upload** (≈1 day, approved plan exists) — users.avatar
    bytea ~256 px Pillow re-encode, 1–2 MB cap.

## Explicitly NOT planned (and why)

- Multi-tenant SaaS / org accounts — you are the company; multi-tenancy
  triples auth/admin complexity before revenue justifies it.
- Real-time WebSocket updates — polling + PRG is fine at this scale.
- Public REST API — the client portal + CSV/NDJSON exports cover
  integrations until a paying client demands otherwise.

## Deploy sequence (unchanged)

local run (seed.py → seed-admin? NO: locally seed.py is fine; in prod
only seed-admin) → push GitHub → Render Blueprint → SMTP env vars →
Tier 0 checklist → invite 2–3 pilot annotators from the Team page →
run one real micro-project end to end (import → label → review →
payout CSV) → then start Tier 1.
