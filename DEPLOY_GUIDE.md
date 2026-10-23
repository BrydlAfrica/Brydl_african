# Brydl Deployment Guide — GitHub push + PostgreSQL, step by step

A first-time, click-by-click walkthrough. Do **Part 1** once (push the
code to GitHub), then **Part 2** (deploy on Render — PostgreSQL is
provisioned for you), then **Part 3** (create the admin and verify).
Estimated total time: ~30 minutes.

---

## Part 1 — Push the code to GitHub

### 1.1 Install Git (once per machine)

| OS | How |
| --- | --- |
| Windows | Download from <https://git-scm.com/download/win> → install with defaults (it includes Git Bash) |
| macOS | `xcode-select --install` (or `brew install git`) |
| Linux | `sudo apt install git` |

Open a terminal and set your identity (once):

```bash
git config --global user.name "Your Name"
git config --global user.email "you@brydl.ng"
```

### 1.2 Create the GitHub repository

1. Sign up / sign in at <https://github.com>.
2. Top-right **+** → **New repository**.
3. Repository name: `brydl-dashboard`.
4. Visibility: **Private** (recommended — it's your business's source code).
5. **Leave every checkbox unticked** — do NOT add a README, .gitignore
   or license. The repo must be empty or the first push will be
   rejected.
6. **Create repository**. Keep the page open — you'll see the repo URL,
   e.g. `https://github.com/yourusername/brydl-dashboard.git`.

### 1.3 Get a Personal Access Token (your git "password")

GitHub no longer accepts account passwords for pushes. You need a
Personal Access Token (PAT) — created once, used whenever git asks for
a password:

1. GitHub → click your avatar → **Settings** → (bottom of the left
   menu) **Developer settings**.
2. **Personal access tokens** → **Tokens (classic)** → **Generate new
   token (classic)**.
3. Note: `brydl-laptop` · Expiration: 90 days (or longer).
4. Scopes: tick **repo** (full control of private repositories).
5. **Generate token** → **copy it immediately** (you will never see it
   again). It looks like `ghp_xxxxxxxxxxxxxxxxxxxx`.

> Alternative: install the GitHub CLI (`winget install GitHub.cli` /
> `brew install gh`) and run `gh auth login` — it handles tokens for
> you. Everything below works the same.

### 1.4 Push the project

Download the `brydl-dashboard` folder from the workspace to your
computer, then in a terminal:

```bash
cd path/to/brydl-dashboard

git init
git add .

# sanity check — you should see ~85 files and NO instance/, NO backups/
git status

git commit -m "Brydl Dashboard - production ready"
git branch -M main
git remote add origin https://github.com/<yourusername>/brydl-dashboard.git
git push -u origin main
```

When prompted:

- **Username**: your GitHub username
- **Password**: paste the **PAT** (not your GitHub password)

> Windows tip: Git Credential Manager usually opens a browser window
> instead — sign in there and it remembers you.

### 1.5 Verify the push

Open `https://github.com/<yourusername>/brydl-dashboard` — you should
see: `app.py`, `models.py`, `render.yaml`, `migrations/`, `tests/`,
`static/brand/` (logo + hero), and **no** `instance/` or `backups/`
folders (the database and secrets stay local, by design).

### 1.6 Everyday workflow from now on

Every push to `main` auto-deploys to Render:

```bash
git add -A
git commit -m "describe the change"
git push
```

### Push troubles

| Error | Fix |
| --- | --- |
| `Authentication failed` | You typed your GitHub password — use the PAT. Make sure it has the `repo` scope. |
| `remote origin already exists` | `git remote set-url origin https://github.com/<you>/brydl-dashboard.git` |
| `rejected (non-fast-forward)` | The GitHub repo wasn't empty. On GitHub, delete the repo and recreate it empty (§1.2), then push again. |
| `Please tell me who you are` | Run the two `git config --global` commands from §1.1. |

---

## Part 2 — Supabase PostgreSQL Database Setup

Brydl uses **Supabase PostgreSQL** for robust, persistent database storage, zero-downtime backups, and connection pooling:

### 2.1 Create Supabase Project

1. Navigate to <https://supabase.com> and sign in / create an account.
2. Click **New Project**:
   - **Name**: `brydl-production` (or `brydl-pilot`)
   - **Database Password**: Generate a secure password and save it in your password manager.
   - **Region**: Select `EU (Frankfurt)` or `EU (London)` for optimal latency to African traffic.
3. Click **Create new project** and wait ~1 minute for provisioning.

### 2.2 Get Connection String URI

1. In Supabase Dashboard, go to **Project Settings** (gear icon) → **Database**.
2. Scroll to **Connection string** → click the **URI** tab.
3. Select **Mode**:
   - **Transaction Pooler (Port 6543)** — *Recommended for web service / serverless*:
     `postgresql://postgres.[project-ref]:[YOUR-PASSWORD]@aws-0-[region].pooler.supabase.com:6543/postgres?sslmode=require`
   - **Session / Direct (Port 5432)** — *For local migrations and CLI operations*:
     `postgresql://postgres:[YOUR-PASSWORD]@db.[project-ref].supabase.co:5432/postgres?sslmode=require`

### 2.3 Configure `DATABASE_URL` on Render Web Service

1. On Render Dashboard (<https://dashboard.render.com>) → your `brydl-dashboard` web service → **Environment**.
2. Set `DATABASE_URL` to your Supabase Transaction Pooler URI (ensure password is inserted and `?sslmode=require` is present).
3. Set `REDLAB_ENABLED=true` and `REDLAB_FERNET_KEY`.
4. Click **Save Changes** — Render automatically builds and runs `flask --app app db upgrade` to create all tables and the `redlab` schema.

Both look like `postgres://brydl_user:PASSWORD@dpg-xxx/brydl` — the app
rewrites the legacy `postgres://` scheme automatically; paste either
one as-is into `DATABASE_URL`.

### 2.4 First migration (manual path only)

The Blueprint's build command does this automatically. On the manual
path, run it once from your machine against the **External** URL:

```bash
cd path/to/brydl-dashboard
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# macOS / Linux
export DATABASE_URL="<External Database URL>"
# Windows (cmd):  set DATABASE_URL=<External Database URL>
# Windows (PowerShell):  $env:DATABASE_URL="<External Database URL>"

flask --app app db upgrade      # creates the schema, stamps the version
```

### 2.5 PostgreSQL pitfalls

| Pitfall | Consequence / fix |
| --- | --- |
| Web service in a different region from the database | Internal URL won't resolve. Put both in Frankfurt (recreate the service). |
| Using the External URL inside the web service | Works, but slower and metered bandwidth — the service should use the Internal URL. |
| `BRYDL_AUTO_SCHEMA` not set to `0` (manual path) | The app would create tables at boot, then migrations would collide (`table already exists`). Fix: set the env var to `0`, and run `flask --app app db stamp head` once against the database to mark it current. |
| Free-plan database silently expiring | **Render's free PostgreSQL is deleted after ~30 days.** See §3.3. |
| App log shows `postgres://` scheme error | Won't happen — config.py rewrites it to `postgresql://` (tested). |

---

## Part 3 — First admin, backups, verification

### 3.1 Create the admin (from your machine)

```bash
# still in the project venv, DATABASE_URL exported (§2.4)
flask --app app seed-admin --email you@brydl.ng
# password prompt: min 8 chars — use a long unique passphrase
```

Rerunning it on purpose fails with "An active admin already exists" —
that's the guard working. All later accounts come from the app's
**Team** page.

### 3.2 Backups — two engines, two tools

The bundled `flask --app app backup create` family snapshots the
**SQLite** dev database only. Point it at a `DATABASE_URL` that starts
with `postgres` and it refuses on purpose — a file copy is not a real
PostgreSQL backup. In production use `pg_dump` (comes with any
PostgreSQL client install) against the **External** Database URL:

```bash
mkdir -p backups
# macOS/Linux — one snapshot; Windows: run the same without $(date ...)
pg_dump "$( … paste the External Database URL … )" \
        --format=custom --no-privileges --no-owner \
        --file "backups/brydl-$(date +%F).dump"
```

Download the `.dump` file off-machine (email/drive/second disk). To
inspect or restore against a fresh database:

```bash
pg_restore --list backups/brydl-YYYY-MM-DD.dump        # contents check
pg_restore --clean --if-exists --dbname "<External Database URL>" \
           backups/brydl-YYYY-MM-DD.dump               # full restore
```

Alternatives: paid Render databases include managed daily backups
(database page → *Backups*), and Render shell has `pg_dump`/`pg_restore`
preinstalled if you ever need a snapshot without a local client.
Keep the weekly rhythm regardless of which tool you use.

### 3.3 The 30-day clock (important)

Render's **free** PostgreSQL expires after ~30 days and its contents
are deleted. Sequence that keeps you safe:

1. Deploy now on free — rehearse with test data only.
2. Before real annotators start: Render dashboard → **brydl-postgres**
   → **Change plan** → e.g. `basic-256mb` (~$6/mo). Data carries over,
   the connection string stays the same, the service reconnects on the
   next deploy. Nothing in the app changes.
3. Keep taking weekly `pg_dump` snapshots regardless (§3.2).

### 3.4 Smoke test (10 minutes)

- Open `https://<your-service>.onrender.com/` — landing page + boot
  animation play.
- Sign in with the seeded admin → **Team** → create one real
  annotator → their one-off password signs them in.
- Create a test project → upload 3 tasks → assign → (second browser,
  annotator) submit → approve with a QA score.
- **Payments** → generate payout → **Bank batch CSV** downloads →
  **Audit** shows every step with your name on it.
- Visit a nonsense URL → branded 404. Logs tab shows
  `REQ GET /... -> 404` with a request id.
- Phone check: open the site on mobile → "Add to Home screen" → the
  PWA installs.

---

## Quick reference — what lives where

| Thing | Where |
| --- | --- |
| Code + auto-deploy | GitHub `main` branch → Render web service |
| Schema changes | Edit `models.py` → `flask --app app db migrate -m "…"` locally → commit → push (build applies it) |
| Production DB env var | `DATABASE_URL` (injected by Blueprint / set manually) |
| Local admin commands | Same `DATABASE_URL`, External Database URL, from your machine |
| Secrets | `SECRET_KEY` generated by Render; SMTP creds added as env vars when you have a provider |
| Logs | Render dashboard → web service → Logs (`REQ`/`AUDIT` lines, request ids) |
