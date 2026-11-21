"""First-admin bootstrap CLI.

    flask --app app seed-admin --email you@brydl.ng
    SEED_ADMIN_PASSWORD=... flask --app app seed-admin --email you@brydl.ng

Creates the FIRST admin account. Safety rules:
- Refuses when any admin already exists — additional admins are managed
  from the app's Team page where every action is audited.
- Refuses weak (< 8 chars) or oversized (> 128 chars) passwords.
- Refuses an email that already belongs to any account.

The password can come from --password, the SEED_ADMIN_PASSWORD
environment variable (non-interactive deploys), or an interactive
hidden prompt. The creation is written to the audit trail as
``system.seed_admin``.
"""

import os
import re

import click
from flask.cli import with_appcontext

from audit import log_event
from extensions import db
from models import User

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@click.command("seed-admin")
@click.option(
    "--email", default=None, help="Admin email (prompted if omitted)."
)
@click.option(
    "--password",
    default=None,
    help=(
        "Admin password (recommended: omit and use the hidden prompt, or"
        " set SEED_ADMIN_PASSWORD for non-interactive deploys)."
    ),
)
@with_appcontext
def seed_admin_command(email: str | None, password: str | None):
    """Create the first admin account (refuses if any admin exists)."""
    existing_admins = db.session.scalar(
        db.select(db.func.count(User.id)).where(
            User.role == "admin", User.is_active.is_(True)
        )
    )
    if existing_admins:
        raise click.ClickException(
            "An active admin already exists — manage further accounts"
            " from the app's Team page (sign in as an admin first)."
        )

    email = (email or "").strip().lower()
    if not email:
        email = click.prompt("Admin email", type=str).strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 255:
        raise click.ClickException(
            f"{email!r} is not a usable email address."
        )
    if db.session.scalar(
        db.select(db.func.count(User.id)).where(User.email == email)
    ):
        raise click.ClickException(
            f"{email} already belongs to an account. Sign in instead or"
            " choose another address."
        )

    password = (
        password
        or os.environ.get("SEED_ADMIN_PASSWORD")
        or click.prompt(
            "Admin password (min 8 characters)", hide_input=True
        )
    ).strip()
    if not 8 <= len(password) <= 128:
        raise click.ClickException(
            "Password must be between 8 and 128 characters."
        )

    admin = User(name="Admin", email=email, role="admin")
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()
    log_event(
        "system.seed_admin",
        actor_name="seed-admin",
        entity_type="user",
        entity_id=admin.id,
        detail={"email": email},
        commit=True,
    )
    click.echo(f"Admin account created: {email}")
    click.echo("Next: sign in and use the Team page to add annotators.")


@click.command("add-target")
@click.option("--project-id", required=True, type=int, help="Target project ID")
@click.option("--name", required=True, type=str, help="Target display name")
@click.option("--endpoint", required=True, type=str, help="Model endpoint URL")
@click.option("--model", required=True, type=str, help="Model ID/name")
@click.option("--api-key", required=True, type=str, help="Client API key/token")
@with_appcontext
def add_target_command(project_id: int, name: str, endpoint: str, model: str, api_key: str):
    """Register and connect an LLM model target for a red-team project."""
    from models import Project, utc_now
    from redlab.models import Target
    from redlab.crypto import encrypt_secret, mask_key_fingerprint
    from redlab.ssrf import validate_target_url

    project = db.session.get(Project, project_id)
    if not project:
        raise click.ClickException(f"Project #{project_id} not found.")

    is_valid, _, reason = validate_target_url(endpoint)
    if not is_valid:
        raise click.ClickException(f"Invalid endpoint URL: {reason}")

    ciphertext = encrypt_secret(api_key)
    fp = mask_key_fingerprint(api_key)

    tgt = Target(
        project_ref=str(project.id),
        name=name,
        endpoint=endpoint,
        model_name=model,
        api_key_ciphertext=ciphertext,
        key_fingerprint=fp,
        ssrf_validated_at=utc_now(),
        active=True,
    )
    db.session.add(tgt)
    db.session.commit()
    click.echo(f"Successfully connected target '{name}' ({model}) to Project #{project.id} [Key: {fp}]")
