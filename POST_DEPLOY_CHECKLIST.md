# First-day operations checklist (after the Render deploy)

Five settings that turn the deployed app into a functioning operation.
Do them once, in this order. Times are honest estimates.

---

## 1. SMTP — password resets become real emails (~15 min)

The app reads these environment variables (config.py):

| Key | Value (Gmail example) |
| --- | --- |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USERNAME` | `yourname@gmail.com` (full address) |
| `SMTP_PASSWORD` | the 16-character app password (below) |
| `MAIL_FROM` | `Brydl <yourname@gmail.com>` |

**Get the app password (once):**
1. Google Account → Security → turn on **2-Step Verification**.
2. Go to <https://myaccount.google.com/apppasswords> → app name
   "brydl" → **Create**.
3. Copy the 16-character password Google shows (you will not see it
   again). That string is `SMTP_PASSWORD` — NOT your normal password.

**Set the variables:**
- *Locally:* in the terminal BEFORE `python app.py`:
  ```bash
  export SMTP_HOST=smtp.gmail.com SMTP_PORT=587
  export SMTP_USERNAME=yourname@gmail.com
  export SMTP_PASSWORD='xxxxxxxxxxxxxxxx'
  export MAIL_FROM='Brydl <yourname@gmail.com>'
  ```
- *On Render:* your service → **Environment** → **Add Environment
  Variable** for each row → **Save** (it redeploys automatically).
  Type `SMTP_PASSWORD` directly into the dashboard — never into
  render.yaml or the repo.

Test: sign in → logout → **Forgot password** → the reset email
arrives. (With SMTP unset, the link goes to the app log instead —
that is the offline fallback, still fully functional.)

> Gmail caps bulk sending (~500/day). If annotators grow past that,
> switch to Brevo/Resend free tiers — same five variables, different
> host/credentials, zero code changes.

## 2. Production admin — seed-admin (~5 min)

Render's free web service has no shell, so run it from YOUR machine
against the production database (one-time setup):

```bash
cd path/to/brydl-dashboard
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Render → brydl-postgres → Connections → EXTERNAL Database URL
export DATABASE_URL="<paste the External Database URL>"
flask --app app seed-admin --email you@brydl.ng
#    → type a long passphrase at the hidden prompt
```

Strong passphrase recipe: 4–5 random words + a digit + a symbol
(`canopy-lion-yam-7-Drums!`). The command refuses to run twice
("an active admin already exists") — that is the guard working.
Everyone else is created from the app's **Team** page.

## 3. Free uptime monitor on /login (~10 min)

1. Sign up at <https://uptimerobot.com> (free 50 monitors).
2. **Add New Monitor** → type **HTTP(s)**.
3. Friendly name: `Brydl production`.
4. URL: `https://<your-service>.onrender.com/login`  ← `/login` is the
   health endpoint (no auth needed), same as Render's health check.
5. Interval: 5 minutes (free default) → **Create Monitor**.

You now get an email the moment production stops answering. Bonus:
the pings keep the free instance warm, which removes the ~50 s
cold-start for the first annotator of the hour.

## 4. Weekly pg_dump — the 30-day clock (~10 min to set up)

Render's FREE PostgreSQL is deleted after ~30 days. Set the reminder:

1. Google Calendar → **Create** → "Brydl: pg_dump production backup".
2. Repeat **weekly** (pick a quiet morning), notification popup.
3. Paste this into the event description so future-you just copies it:

   ```bash
   mkdir -p backups
   pg_dump "<External Database URL>" --format=custom \
           --no-privileges --no-owner \
           --file "backups/brydl-$(date +%F).dump"
   ```
   (Windows PowerShell: `"backups/brydl-$(Get-Date -F yyyy-MM-dd).dump"`)

`pg_dump` comes with any PostgreSQL install (`postgresapp`/installer
from postgresql.org). After the first run, eyeball the contents once:

```bash
pg_restore --list backups/brydl-YYYY-MM-DD.dump | head -30
```

Keep each dump off-machine (Drive/email). The Internal URL stays in
Render's env vars — backups always use the **External** URL.

## 5. USD_TO_NGN_RATE — today's Naira rate (~2 min)

Payout math is USD; the Naira figure is display-only and comes from
this knob (default 1600).

1. Check today's rate (Google "USD to NGN", or your bank/Paystack
   rate — use the rate YOU actually pay).
2. Render → your service → **Environment** → add:
   - key: `USD_TO_NGN_RATE` value: e.g. `1534`
3. **Save** (redeploys; annotator dashboards then show the new ₦
   figures). Locally: `export USD_TO_NGN_RATE=1534` before starting.

Update it whenever the rate moves meaningfully — that is what the knob
is for; no deploy, no code.

---

## 5a. Verify the migrations landed (first deploy only)

In the build log, look for **9 `Running upgrade … -> …` lines**
ending with `-> c7d21b8a45e9, api + jobs (Phase 10)`. That is chain 17
head — schema is complete. Missing lines mean the upgrade command
did not run (check the Build Command in Render settings — it must be
`pip install -r requirements.txt && flask --app app db upgrade`).

---

## 6. (Do not skip) Make the Terms & Conditions yours

`templates/annotator/terms.html` is a solid draft, but it is the
contract annotators accept. Edit it to your real conditions, then
commit + push. If you ever change it LATER after people accepted,
also set `BRYDL_TERMS_VERSION=2.0` (env var) so every annotator is
re-prompted to accept the new version.

---

**Order for day one:** deploy → 1 (SMTP) → 2 (admin) → 6 (terms) →
3 (monitor) → 4 (calendar) → 5 (rate) → invite 2–3 pilot annotators
from the Team page and run one real micro-project end to end.
