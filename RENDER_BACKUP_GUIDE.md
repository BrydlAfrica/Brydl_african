# Finding backups on Render + manual `pg_dump` fallback

If Render's UI doesn't show a Backups tab on your free Postgres, it's
usually one of three things:

1. The database was created as an **Internal Database** on an older
   Render plan — free-tier Postgres created in 2024+ has a
   **Backups** section, but older ones sometimes don't.
2. You're looking at the **web service** page, not the **Postgres
   service** page. They're two separate services in your Render
   Dashboard list.
3. You're on the *free trial* of a paid plan that has Backups disabled
   until you add a payment method (unlikely).

Below: exact click-by-click navigation, and a guaranteed command-line
fallback that works from any plan (including free) so you have a backup
before pushing v13.

---

## Part 1 — Find the Postgres service in Render

1. Go to [dashboard.render.com](https://dashboard.render.com) and sign
   in.
2. The left sidebar has **Services** (shows the web service) and
   **PostgreSQL** (shows the database). Click **PostgreSQL**.
   - If you don't see a **PostgreSQL** heading, your DB was created
     as an *internal* DB attached to the web service. Click your web
     service → scroll down to the **Connections** section on the right
     → click the database name link under "Connected PostgreSQL" — that
     opens the DB service page.
3. Once you're on the database's page, look at the horizontal tab bar
   under the database name: **Info · Connections · Backups · Events ·
   Settings**.
4. Click **Backups** → **Manual Backups** → **Create Backup**. When it
   finishes (spinner goes away), a **Download** button appears.

### If you genuinely do NOT see a Backups tab

That means your plan/region doesn't support one-click backups. No
problem — Part 2 is a guaranteed manual backup that takes 3 minutes
and produces the same `.sql` file.

---

## Part 2 — Manual backup from the Render Shell (always works)

You use the *web service*'s shell (which already has psycopg2 + network
access to the DB) to run `pg_dump`. If `pg_dump` isn't on the image,
I've included a pure-Python fallback.

### 2.1 Open the web service shell

1. Render Dashboard → click your **web service** (not the database).
2. Top-right corner → **Shell**. A black terminal opens in your
   browser, and after a few seconds you'll see a prompt like:
   ```
   app@some-id-1234:/opt/render/project/src$
   ```

### 2.2 Copy your DATABASE_URL

From inside the shell:

```bash
echo $DATABASE_URL
```

It prints something like:
```
postgres://brydl:abc123@dpg-xxx.render.com:5432/brydl_abc
```
(Copy that whole line; it contains the username, password, host, port,
and DB name.)

### 2.3 Try pg_dump first (fastest)

```bash
# Install postgresql client tools inside the running container.
apt-get update -qq && apt-get install -y -qq postgresql-client
ts=$(date +%Y-%m-%d-%H%M)
pg_dump "$DATABASE_URL" | gzip > /tmp/brydl-pre-v13-$ts.sql.gz
ls -lh /tmp/brydl-pre-v13-*.sql.gz
```

If that succeeds you'll see a file like
`/tmp/brydl-pre-v13-2026-10-07-1430.sql.gz` (probably 10–500 KB). Skip
to 2.5.

If `apt-get` fails (free containers can block outbound package
installs sometimes) or `pg_dump` errors with "command not found", use
the Python fallback below.

### 2.4 Python fallback (no apt needed)

Run this from the same shell — it dumps every table through SQLAlchemy
to a JSON file. Not quite as full-featured as a raw `pg_dump`, but it
captures all your data (users, projects, tasks, submissions, payments,
settings, audit log, notifications):

```bash
cd /opt/render/project/src
python - <<'PY'
import gzip, json, os, datetime as dt
from flask import current_app
from app import create_app
from extensions import db
from sqlalchemy import inspect, text
from models import *  # noqa: F401,F403 — register all models

app = create_app()
ts = dt.datetime.utcnow().strftime("%Y-%m-%d-%H%M")
out_path = f"/tmp/brydl-pre-v13-{ts}.json.gz"

with app.app_context():
    insp = inspect(db.engine)
    dump = {"dumped_at": ts, "tables": {}}
    for table in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns(table)]
        rows = []
        with db.engine.connect() as conn:
            rp = conn.execute(text(f'SELECT * FROM "{table}"'))
            for r in rp.mappings():
                row = {}
                for k, v in dict(r).items():
                    if isinstance(v, (dt.datetime, dt.date)):
                        v = v.isoformat()
                    if isinstance(v, (bytes, bytearray)):
                        import base64
                        v = {"__bytes_b64__": base64.b64encode(v).decode()}
                    row[k] = v
                rows.append(row)
        dump["tables"][table] = {"cols": cols, "rows": rows}
    with gzip.open(out_path, "wt") as f:
        json.dump(dump, f, indent=2, default=str)
    print(f"WROTE {out_path}  "
          f"{sum(len(t['rows']) for t in dump['tables'].values())} rows total")
PY

ls -lh /tmp/brydl-pre-v13-*.json.gz
```

### 2.5 Get the file off the container

The shell's filesystem is ephemeral (gone when the service restarts),
so you need to print/copy the backup out **before you close the shell**.
The simplest route is base64 over the terminal:

```bash
# Pick the file you created in 2.3 or 2.4:
FILE=/tmp/brydl-pre-v13-*.sql.gz    # or *.json.gz
FILE=$(ls -t $FILE | head -1)
echo "FILE IS: $FILE"
echo "--- COPY EVERYTHING BETWEEN THE MARKERS ---"
echo "-----BEGIN BACKUP $FILE-----"
base64 -w0 "$FILE"
echo
echo "-----END BACKUP-----"
```

On your own laptop:
1. Copy the entire base64 blob (between the two `-----` lines, not the
   lines themselves).
2. Paste it into a text editor and save as `brydl-pre-v13.b64`.
3. Decode locally:

   **Mac/Linux (Git Bash):**
   ```bash
   base64 -d brydl-pre-v13.b64 > brydl-pre-v13.sql.gz
   # or .json.gz, depending which method you used
   ls -lh brydl-pre-v13.sql.gz
   ```

   **Windows PowerShell:**
   ```powershell
   $b64 = Get-Content brydl-pre-v13.b64 -Raw
   [IO.File]::WriteAllBytes("brydl-pre-v13.sql.gz", [Convert]::FromBase64String($b64))
   ```

Open the `.gz` with 7-Zip / Archive Utility / `gunzip` to verify it's
non-empty (the `.sql` file should be readable text starting with
`-- PostgreSQL database dump`; the `.json.gz` should start with
`{"dumped_at":...}`).

---

## Part 3 — Quick restore (if v13 breaks your data)

Keep these handy even though you probably won't need them:

- **SQL dump** (from `pg_dump`): restore with
  ```bash
  gunzip < brydl-pre-v13.sql.gz | psql "$DATABASE_URL"
  ```
  (Run from a fresh Render shell after restoring.)
- **JSON dump** (from the Python fallback): I'll ship a small restore
  script alongside if needed, but for a full rollback the simpler path
  is to restore the SQL backup above. If you had to use the JSON
  fallback because you couldn't install pg tools, tell me and I'll
  push a `flask db-load-json` CLI command in 2 minutes.

---

## Part 4 — Now deploy v13 safely

Once you have the backup file saved on your laptop, push code:

```bash
git push
git push origin v13.0
```

If the deploy fails because of the missing Alembic stamp (most likely
for v1.0 seed.py upgraders), open a **new** Render shell on the web
service after the failed deploy and run:

```bash
flask --app app db stamp f1a94c3b7e20
flask --app app db upgrade
```

Then ⋮ → **Restart service** on the web service. That's the only prod
intervention you need — everything else is automatic.
