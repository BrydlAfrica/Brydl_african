# Run Brydl locally + push to GitHub — step by step

You do **NOT** need to change any code in the repository to run it on
your own machine. Everything below is standard Python + git setup. The
app works out of the box with an SQLite file database in `instance/`
(that folder is already in `.gitignore` so it never gets committed).

Estimated time: **~15 minutes** the first time.

---

## 0. Prerequisites (install once per laptop)

You need three things:

| Tool | Version | How to install |
|---|---|---|
| **Python** | 3.11.x (or 3.12 works; 3.13 is what tests use in this sandbox) | [python.org/downloads](https://www.python.org/downloads/) or `brew install python@3.11` on Mac |
| **Git** | any recent | [git-scm.com/downloads](https://git-scm.com/downloads) |
| **A GitHub account** | — | [github.com/signup](https://github.com/signup) |

Verify from a terminal (Command Prompt on Windows, Terminal on Mac/Linux):

```bash
python --version    # should say 3.11.x or 3.12.x
git --version       # any version is fine
```

On Windows, use **Git Bash** (it comes with Git for Windows) or PowerShell
— the commands below work in both.

---

## Part 1 — Get the code onto your own GitHub account

### 1.1 Download the code from the workspace

Because this sandbox doesn't have a remote, copy the whole
`brydl-dashboard` folder out of the workspace to your home directory,
**or** run the commands below directly inside `/home/user/brydl-dashboard`.

If you've been working in this Arena workspace, the folder is already a
fully-formed git repo at commit `v13.0` / `f2bd646`. You can zip it from
the file tree and unzip on your own machine — the `.git/` folder inside
preserves all history.

### 1.2 Tell git who you are (run once on your laptop)

```bash
git config --global user.name "Your Name"
git config --global user.email "you@your-email.com"
```

Use the **same email** your GitHub account uses (check at
github.com/settings/emails).

### 1.3 Create the empty repo on GitHub

1. Sign in to github.com.
2. Top-right **+** → **New repository**.
3. **Repository name:** `brydl-dashboard`.
4. Choose **Private** (recommended — it's your business data/tooling).
5. **UNCHECK EVERY BOX** on the next screen (no README, no .gitignore, no
   license — if you leave them checked the first push gets rejected
   because the repo isn't empty).
6. Click **Create repository**.
7. On the page that loads, copy the repo URL. It will look like either:
   - HTTPS: `https://github.com/YOUR_USERNAME/brydl-dashboard.git`
   - SSH:   `git@github.com:YOUR_USERNAME/brydl-dashboard.git`

### 1.4 Link your local folder to GitHub and push

Open a terminal, `cd` into the `brydl-dashboard` folder, then:

```bash
# cd path/to/brydl-dashboard

# (only needed if you copied the folder out of this sandbox)
git status                       # should say "on branch main, nothing to commit"

# Add GitHub as the remote called "origin"
git remote add origin https://github.com/YOUR_USERNAME/brydl-dashboard.git
# (use the SSH URL instead if you prefer SSH keys)

# Rename the default branch to main (already is, but harmless)
git branch -M main

# Send all code + history to GitHub
git push -u origin main

# Also push the v13.0 release tag
git push origin v13.0
```

When you run `git push`, git will ask for your GitHub credentials.
- If you use HTTPS, paste a **Personal Access Token** (classic, scopes
  `repo`) as the password. Create one at
  github.com → Settings → Developer settings → Personal access tokens →
  Tokens (classic).
- If you use SSH, set up a key once (`ssh-keygen`, add the `.pub` file to
  github.com/settings/keys) and you'll never be asked for a password
  again.

Refresh your GitHub repo page — you should see all 15 commits, the
folders (`app.py`, `routes/`, `templates/`, `tests/`, etc.) and the
`v13.0` tag in the Releases/tags tab.

**Do this whenever you have new code to send up:**

```bash
git status                       # see what changed
git add -A                       # stage all changes
git commit -m "short message"    # save a snapshot
git push                         # send to GitHub
```

---

## Part 2 — Run Brydl on your laptop (first time)

### 2.1 Open a terminal in the project folder

```bash
cd path/to/brydl-dashboard
```

### 2.2 Create a virtual environment (once)

**Mac / Linux / Git Bash:**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows PowerShell:**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

You'll know it worked because your prompt shows `(.venv)` at the start.

### 2.3 Install the dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

That installs Flask, SQLAlchemy, WTForms, gunicorn, psycopg2, cryptography,
sentry-sdk, markdown/sanitizer, etc. — about ~30 packages, takes 1–2 min.

### 2.4 Create the database and start the server

```bash
# SQLite database auto-creates in instance/brydl.db the first run.
python app.py
```

You'll see something like:

```
* Running on http://127.0.0.1:5000
* Debug mode: on
```

Leave this terminal window open — closing it stops the server. Open a
**second terminal** for the commands below.

### 2.5 Create your first admin account

In the second terminal, activate the venv again (so `flask` is found):

**Mac/Linux:**
```bash
source .venv/bin/activate
```

**Windows PowerShell:**
```powershell
.venv\Scripts\Activate.ps1
```

Then run:

```bash
flask --app app seed-admin --email your@email.com
```

It will prompt for a password (min 8 chars). Pick something real — this
is **your** login.

> *Optional:* if you want a playground full of sample data (5 fake
> annotators, a $0.10/task project, 10 sample tasks), run
> `python seed.py` **before** you log in. It creates
> `admin@brydl.com / admin123` plus demo annotators. Skip this if you
> want a clean slate.

### 2.6 Log in

1. Open your browser and go to **http://127.0.0.1:5000** (or
   http://localhost:5000).
2. You'll be redirected to `/login`.
3. Sign in with the email/password you just created.
4. You land on the Admin dashboard. KPI tiles will show zero until you
   create projects + upload tasks, which is normal.

### 2.7 Stop the server

Back in the first terminal (the one running `python app.py`), press
`Ctrl+C`. To start it again:

```bash
source .venv/bin/activate    # only needed if you opened a new terminal
python app.py
```

The database file lives at `instance/brydl.db`. Delete that file and
re-run `flask seed-admin` whenever you want a fresh reset.

---

## Part 3 — Configuration you can change (all optional, none required for local use)

The app reads configuration from environment variables. Locally you
don't *need* any of these — everything falls back to sensible defaults.
You set them in the terminal before `python app.py` (or put them in a
`.env` file which `.gitignore` already excludes):

| Variable | Default | What it does |
|---|---|---|
| `SECRET_KEY` | auto-generated in `instance/secret_key` | Signs session cookies. Set a fixed value if you want sessions to survive `instance/` wipes. |
| `USD_TO_NGN_RATE` | `1600` | Fallback Naira per $1 if you haven't set it in Admin → Settings. |
| `BRYDL_DISABLE_JOBS` | (unset) | Set to `1` to turn off the background scheduler (recommended locally — it's already off in tests). |
| `SMTP_HOST` `SMTP_PORT` `SMTP_USERNAME` `SMTP_PASSWORD` `MAIL_FROM` | none | Optional real email for password-reset links. Without them reset links just print to the terminal log. |
| `SENTRY_DSN` | none | Optional error tracking — leave blank locally. |
| `FLASK_ENV` | `development` | Set to `production` only on Render. |
| `BANK_ENC_KEY` | derived from `SECRET_KEY` | Encrypts bank account numbers at rest. Leave alone unless you rotate SECRET_KEY intentionally. |

Example dev run with jobs disabled and a custom FX rate:

```bash
export BRYDL_DISABLE_JOBS=1
export USD_TO_NGN_RATE=1750
python app.py
```

(On Windows PowerShell use `$env:BRYDL_DISABLE_JOBS = "1"` etc.)

## Part 4 — Code changes — you don't need any right now

You can click around as an admin, create a project, upload CSV tasks,
add annotators, see payments, try the PM quiz editor, etc. **No code
changes are required** to run locally; the SQLite + Flask dev server is
the full application.

If you ever want to tweak code:
1. Edit the file (e.g. add a field to a model).
2. If you changed **models.py**, generate a migration:
   ```bash
   flask --app app db migrate -m "describe what changed"
   flask --app app db upgrade
   ```
3. Test it: `python app.py` and click around, plus run the suite:
   ```bash
   rm -rf instance backups
   PYTHONPATH=$PWD BRYDL_DISABLE_JOBS=1 python -m pytest tests/ -q
   ```
4. Commit + push:
   ```bash
   git add -A
   git commit -m "what you changed"
   git push
   ```

## Part 5 — Deploying from GitHub to Render (later, when you're ready)

Once the code is on GitHub:

1. Sign up at [render.com](https://render.com) (free tier works).
2. **New + → Blueprint** → connect your GitHub → pick `brydl-dashboard`.
3. Render reads `render.yaml` and will provision:
   - A free Postgres database (`brydl-postgres`).
   - A Python web service running gunicorn with auto-deploys on every
     push to `main`.
4. After first deploy completes, open the service's **Shell** tab and
   run:
   ```bash
   flask --app app seed-admin --email your@email.com
   ```
   (Enter your password when prompted — this creates the first admin in
   the production DB.)
5. Open the `*.onrender.com` URL and log in.
6. **Don't forget backups** — see my previous message. Render free
   Postgres expires at 90 days; download a manual backup weekly from
   the database's Backups tab until you upgrade.

---

## Troubleshooting the most common issues

- **`command not found: python` / `python3`** — Python isn't installed
  or isn't on PATH. Re-run the installer and check "Add Python to PATH".
- **`pip: command not found` after activating venv** — you're not inside
  the venv. Run the `source .venv/bin/activate` (Mac/Linux) or
  `.venv\Scripts\Activate.ps1` (Windows) step again.
- **`Address already in use` on port 5000** — another copy is running.
  Stop it with Ctrl+C in its terminal, or change port:
  ```python
  # at the very bottom of app.py temporarily
  app.run(debug=True, port=5001)
  ```
- **Changes I make don't appear in the browser** — debug mode auto-reloads
  most files, but if you edit templates hard-refresh (Ctrl/Cmd+Shift+R).
  Model changes need a migration; sometimes deleting `instance/brydl.db`
  and re-running seed-admin is fastest during early dev.
- **`git push` asks for password and rejects it** — you need a Personal
  Access Token (step 1.4 above), not your GitHub password.
- **`psycopg2` fails to build on Apple Silicon Mac** — install
  postgresql first (`brew install postgresql`) then re-run
  `pip install -r requirements.txt`. Locally Brydl uses SQLite, but
  psycopg2 is still a requirement for production deploys so it's in the
  requirements file.
