"""Backup system: consistent, verified snapshots of the Brydl database.

Commands (run from the project root):

    flask --app app backup create            # snapshot + manifest + prune
    flask --app app backup list              # what exists, with checksums
    flask --app app backup restore NAME --yes

How it works (SQLite, the current engine):
- The snapshot uses SQLite's online backup API (sqlite3.Connection.backup),
  which copies the database under a read lock: the result is always a
  consistent point-in-time image even if the web app is mid-write.
- Every snapshot gets a sidecar ``.manifest.json`` with a SHA-256
  checksum, row counts and integrity-check result, so a restore VERIFIES
  the file before touching the live database.
- ``create`` prunes old snapshots down to config.BACKUP_KEEP (default 14).

PostgreSQL (after the Render migration): SQLite file copies no longer
apply. This command family detects a non-SQLite DATABASE_URL and refuses
with instructions, so nobody mistakes a file copy for a real Postgres
backup — use managed daily backups / pg_dump at that point.

Snapshots are written to config.BACKUP_DIR (default ``backups/``, kept
out of git). On Render's free plan the disk is EPHEMERAL: download
snapshots (Render shell -> your object storage / local machine) to keep
an off-site copy. Every create/restore is written to the audit trail.
"""

import hashlib
import json
import os
import shutil
import sqlite3

import click
from flask import current_app

from audit import log_event
from extensions import db
from models import utc_now

# Tables counted into each manifest, in dependency order. Fixed list on
# purpose: a snapshot should describe the known schema, and the SQL is
# built only from these constants (never from user input).
_TRACKED_TABLES = (
    "users",
    "projects",
    "tasks",
    "submissions",
    "payments",
    "audit_logs",
)

BACKUP_GLOB_PREFIX = "brydl-"
BACKUP_SUFFIX = ".db"
MANIFEST_SUFFIX = ".manifest.json"


@click.group("backup")
def backup_cli():
    """Backup, list and restore database snapshots."""


def _backup_dir() -> str:
    """Return the configured backup directory, creating it if needed.

    Returns:
        The absolute path of the backup directory.
    """
    directory = current_app.config.get("BACKUP_DIR") or os.path.join(
        current_app.instance_path, "backups"
    )
    os.makedirs(directory, exist_ok=True)
    return directory


def _sqlite_path() -> str | None:
    """Extract the SQLite file path from the configured database URI.

    Returns:
        The filesystem path of the SQLite database, or None when the app
        is configured for a non-SQLite engine (e.g. PostgreSQL).
    """
    uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if uri.startswith("sqlite:///"):
        return uri.replace("sqlite:///", "", 1)
    return None


def _sha256_of(path: str) -> str:
    """Compute the SHA-256 checksum of a file, streaming.

    Args:
        path: The file to hash.

    Returns:
        The hex digest.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prune(directory: str, keep: int) -> int:
    """Delete the oldest snapshots beyond the retention count.

    Args:
        directory: The backup directory.
        keep: How many snapshots to retain.

    Returns:
        The number of snapshots removed.
    """
    names = sorted(
        name
        for name in os.listdir(directory)
        if name.startswith(BACKUP_GLOB_PREFIX)
        and name.endswith(BACKUP_SUFFIX)
    )
    removed = 0
    for name in names[: max(len(names) - keep, 0)]:
        os.remove(os.path.join(directory, name))
        manifest = os.path.join(
            directory, name[: -len(BACKUP_SUFFIX)] + MANIFEST_SUFFIX
        )
        if os.path.exists(manifest):
            os.remove(manifest)
        removed += 1
    return removed


@backup_cli.command("create")
def create_command():
    """Create a consistent, verified snapshot of the database.

    SQLite runs in WAL mode (extensions.py), so the online backup API
    reads the main file AND the write-ahead log together: transactions
    not yet checkpointed into the main file are still captured. The
    result is a single self-contained file with no sidecars needed.
    """
    db_path = _sqlite_path()
    if db_path is None:
        raise click.ClickException(
            "Automatic file backups only support the SQLite engine. This"
            " app uses an external database — use that engine's native"
            " tool instead (e.g. `pg_dump` or managed backups)."
        )
    if not os.path.exists(db_path):
        raise click.ClickException(
            f"No database file found at {db_path} — nothing to back up."
        )

    directory = _backup_dir()
    keep = int(current_app.config.get("BACKUP_KEEP", 14))
    stamp = utc_now().strftime("%Y%m%d-%H%M%S")
    name = f"{BACKUP_GLOB_PREFIX}{stamp}{BACKUP_SUFFIX}"
    destination = os.path.join(directory, name)

    # Online, lock-consistent copy (safe while the web app is running).
    source = sqlite3.connect(db_path)
    replica = sqlite3.connect(destination)
    try:
        with replica:
            source.backup(replica)
    finally:
        replica.close()
        source.close()

    # Verify the copy before trusting it.
    check = sqlite3.connect(destination)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {
            table: check.execute(
                f"SELECT COUNT(*) FROM {table}"  # noqa: S608 - constant name
            ).fetchone()[0]
            for table in _TRACKED_TABLES
        }
    finally:
        check.close()
    if integrity != "ok":
        os.remove(destination)
        raise click.ClickException(
            f"Snapshot failed its integrity check ({integrity!r}) and was"
            " deleted — nothing was changed."
        )

    size = os.path.getsize(destination)
    checksum = _sha256_of(destination)
    manifest = {
        "file": name,
        "created_at_utc": stamp,
        "size_bytes": size,
        "sha256": checksum,
        "integrity_check": integrity,
        "row_counts": counts,
        "engine": "sqlite",
    }
    with open(
        os.path.join(
            directory, name[: -len(BACKUP_SUFFIX)] + MANIFEST_SUFFIX
        ),
        "w",
        encoding="utf-8",
    ) as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")

    pruned = _prune(directory, keep)
    log_event(
        "system.backup",
        actor_name="system",
        entity_type="database",
        detail={
            "file": name,
            "size_bytes": size,
            "sha256": checksum[:16],
            "row_counts": counts,
            "pruned": pruned,
        },
        commit=True,
    )
    click.echo(f"Created {name} ({size:,} bytes, sha256 {checksum[:16]}…)")
    click.echo(f"Rows: {counts}")
    if pruned:
        click.echo(f"Retention: removed {pruned} old snapshot(s).")


@backup_cli.command("list")
def list_command():
    """List snapshots with their manifest details."""
    directory = _backup_dir()
    names = sorted(
        name
        for name in os.listdir(directory)
        if name.startswith(BACKUP_GLOB_PREFIX)
        and name.endswith(BACKUP_SUFFIX)
    )
    if not names:
        click.echo("No snapshots yet — run `flask --app app backup create`.")
        return
    for name in names:
        manifest_path = os.path.join(
            directory, name[: -len(BACKUP_SUFFIX)] + MANIFEST_SUFFIX
        )
        if os.path.exists(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            click.echo(
                f"{name}  {data.get('size_bytes', 0):>12,} bytes"
                f"  sha256:{str(data.get('sha256', '-'))[:16]}"
                f"  tasks={data.get('row_counts', {}).get('tasks', '?')}"
                f"  subs={data.get('row_counts', {}).get('submissions', '?')}"
                f"  utc={data.get('created_at_utc', '?')}"
            )
        else:
            size = os.path.getsize(os.path.join(directory, name))
            click.echo(
                f"{name}  {size:>12,} bytes  (no manifest — verify"
                " manually before restoring)"
            )


@backup_cli.command("restore")
@click.argument("filename")
@click.option(
    "--yes",
    is_flag=True,
    help="Actually overwrite the live database (otherwise dry-run).",
)
def restore_command(filename: str, yes: bool):
    """Restore a snapshot over the live database (checksum-verified).

    FILENAME is the snapshot name as shown by `backup list`, e.g.
    brydl-20260910-120000.db
    """
    db_path = _sqlite_path()
    if db_path is None:
        raise click.ClickException(
            "Restore only supports the SQLite engine; this app uses an"
            " external database — use its native restore tooling."
        )
    # Path-safety: a bare filename inside the backup dir, nothing else.
    if os.path.basename(filename) != filename or not filename.startswith(
        BACKUP_GLOB_PREFIX
    ) or not filename.endswith(BACKUP_SUFFIX):
        raise click.ClickException(
            "Give the bare name of a snapshot (see `backup list`)."
        )
    backup_path = os.path.join(_backup_dir(), filename)
    if not os.path.exists(backup_path):
        raise click.ClickException(f"Snapshot not found: {filename}")

    manifest_path = os.path.join(
        _backup_dir(), filename[: -len(BACKUP_SUFFIX)] + MANIFEST_SUFFIX
    )
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        actual = _sha256_of(backup_path)
        if actual != data.get("sha256"):
            raise click.ClickException(
                "Checksum mismatch — the snapshot file is corrupted or"
                " was modified. Refusing to restore."
            )
        click.echo(f"Checksum OK ({actual[:16]}…).")
    else:
        click.echo("WARNING: no manifest; restoring without verification.")

    if not yes:
        click.echo(
            "DRY-RUN: nothing written. Re-run with --yes to overwrite the"
            " live database with this snapshot."
        )
        return

    # Atomic restore onto a NEW inode. Two failure modes make an
    # in-place overwrite unsafe with WAL mode (extensions.py):
    # 1. Recent commits live in brydl.db-wal until the next checkpoint —
    #    leaving the sidecars in place would replay PRE-restore
    #    transactions over the restored file (the restore silently
    #    undoes itself).
    # 2. Overwriting the same inode under existing connections means a
    #    later last-connection-close CHECKPOINT scrapes the old WAL back
    #    over the restored bytes (observed as resurrected rows and disk
    #    I/O errors).
    # So: stage a verified copy beside the live file, drop the sidecars
    # (they belong to the discarded database), then os.replace() — an
    # atomic rename to a fresh inode. Connections opened before the
    # swap keep a coherent, now-unlinked old file until they are closed
    # (hence the "restart the web service" guidance below); every NEW
    # connection, including the audit write after dispose, sees exactly
    # the verified snapshot bytes.
    staging_path = db_path + ".restore-tmp"
    shutil.copyfile(backup_path, staging_path)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path + suffix
        if os.path.exists(sidecar):
            os.remove(sidecar)
    os.replace(staging_path, db_path)
    # The CLI process opened pooled connections while booting the app —
    # dispose them so the audit row below is written through a FRESH
    # connection to the restored file.
    db.session.remove()
    current_app.extensions["sqlalchemy"].engine.dispose()
    log_event(
        "system.restore",
        actor_name="system",
        entity_type="database",
        detail={"from_file": filename},
        commit=True,
    )
    click.echo(f"Restored {filename} over the live database.")
    click.echo(
        "IMPORTANT: restart the web service now — running workers keep"
        " pooled database connections that may not see the restored"
        " file until they reconnect."
    )


@backup_cli.command("verify")
@click.argument("filename")
def verify_command(filename: str):
    """Re-verify a snapshot's checksum and SQLite integrity."""
    directory = _backup_dir()
    backup_path = os.path.join(directory, filename)
    if not os.path.exists(backup_path):
        raise click.ClickException(f"Snapshot not found: {filename}")
    manifest_path = os.path.join(
        directory, filename[: -len(BACKUP_SUFFIX)] + MANIFEST_SUFFIX
    )
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        actual = _sha256_of(backup_path)
        state = "OK" if actual == data.get("sha256") else "MISMATCH"
        click.echo(f"checksum: {state} ({actual[:16]}…)")
    else:
        click.echo("checksum: no manifest to compare against")
    connection = sqlite3.connect(backup_path)
    try:
        integrity = connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]
    finally:
        connection.close()
    click.echo(f"sqlite integrity: {integrity}")
