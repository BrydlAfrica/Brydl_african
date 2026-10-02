#!/usr/bin/env python3
"""Development seed data for Brydl Dashboard — DEV ONLY, never production.

Creates (idempotently — re-running skips whatever already exists):
- 1 admin:      admin@brydl.com / admin123
- 5 annotators: annotator1@brydl.com … annotator5@brydl.com /
  annotator123
- 1 sample classification project (pay $0.10/task) with 10 tasks, a few
  carrying gold answers, the first four pre-assigned so two annotators'
  dashboards are alive immediately.

Run from the project root:

    python seed.py

The script REFUSES to run when it detects a production environment
(Render sets the RENDER env var; FLASK_ENV=production also blocks it).
The seeded passwords are deliberately weak placeholders — change the
admin password from the app before any real use.
"""

import os
import sys

from app import create_app
from audit import log_event
from extensions import db
import quizzes
from models import Project, Task, User, QualificationQuiz, utc_now

ADMIN_EMAIL = "admin@brydl.com"
ADMIN_PASSWORD = "admin123"
ANNOTATOR_PASSWORD = "annotator123"

ANNOTATORS = [
    ("Chinedu Okafor", "annotator1@brydl.com"),
    ("Amina Bello", "annotator2@brydl.com"),
    ("Tunde Adeyemi", "annotator3@brydl.com"),
    ("Ngozi Eze", "annotator4@brydl.com"),
    ("Emeka Obi", "annotator5@brydl.com"),
]

SAMPLE_PROJECT = {
    "name": "Product Sentiment — Batch 1",
    "client_name": "Brydl Labs (sample)",
    "task_type": "classification",
    "pay_per_task": 0.10,
    "instructions": (
        "Read each product review and classify its overall sentiment"
        " as positive, negative or neutral.\n\n"
        "- positive: praises the product or recommends it\n"
        "- negative: complains, warns buyers off, or reports a fault\n"
        "- neutral: mixed, factual or purely descriptive\n\n"
        "Judge the overall tone, not single words. When genuinely torn"
        " between two labels, choose the one matching the reviewer's"
        " final sentence."
    ),
}

# (review text, gold label or None). 10 tasks, 3 with gold answers.
SAMPLE_TASKS = [
    (
        "The battery lasts three days on one charge."
        " Absolutely brilliant.",
        "positive",
    ),
    (
        "Arrived cracked and the seller ignored every email I sent.",
        "negative",
    ),
    ("It is a 500ml steel water bottle with a black lid.", None),
    (
        "Setup took ten minutes and the app walked me through"
        " everything.",
        "positive",
    ),
    (
        "The fabric feels cheap and it shrank two sizes in the first"
        " wash.",
        None,
    ),
    ("Bought it on Friday, used it on Saturday. Works as described.", None),
    (
        "Customer support replaced the faulty charger within a day."
        " Impressive.",
        None,
    ),
    (
        "The screen scratches if you look at it wrong. Total waste of"
        " money.",
        None,
    ),
    ("Decent value for the price, though the zip sticks sometimes.", None),
    (
        "Shipping was fast but the colour looks nothing like the photos.",
        None,
    ),
]


def _refuse_production() -> None:
    """Abort unless this is clearly a development environment."""
    if os.environ.get("RENDER"):
        sys.exit(
            "REFUSING to seed: production environment detected (RENDER"
            " env var is set). Seeding is a development-only tool."
        )
    flask_env = os.environ.get("FLASK_ENV", "").strip().lower()
    if flask_env in {"production", "prod"}:
        sys.exit(
            "REFUSING to seed: FLASK_ENV is set to"
            f" {flask_env!r}. Seeding is a development-only tool."
        )


def _get_or_create_user(
    name: str, email: str, password: str, role: str
) -> tuple[User, bool]:
    """Fetch a user by email or create them.

    Args:
        name: Display name.
        email: Lowercase unique email.
        password: Plain-text password to hash on creation.
        role: "admin" or "annotator".

    Returns:
        The user row and whether it was newly created.
    """
    user = db.session.scalar(db.select(User).where(User.email == email))
    if user is not None:
        return user, False
    user = User(name=name, email=email, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    return user, True


def main() -> None:
    """Seed the development database (idempotent)."""
    _refuse_production()
    app = create_app()
    with app.app_context():
        created_users = 0
        admin, made = _get_or_create_user(
            "Brydl Admin", ADMIN_EMAIL, ADMIN_PASSWORD, "admin"
        )
        created_users += int(made)
        annotators = []
        for name, email in ANNOTATORS:
            annotator, made = _get_or_create_user(
                name, email, ANNOTATOR_PASSWORD, "annotator"
            )
            annotators.append(annotator)
            created_users += int(made)

        project = db.session.scalar(
            db.select(Project).where(Project.name == SAMPLE_PROJECT["name"])
        )
        created_project = False
        created_tasks = 0
        assigned_tasks = 0
        if project is None:
            project = Project(
                name=SAMPLE_PROJECT["name"],
                client_name=SAMPLE_PROJECT["client_name"],
                task_type=SAMPLE_PROJECT["task_type"],
                pay_per_task=SAMPLE_PROJECT["pay_per_task"],
                instructions=SAMPLE_PROJECT["instructions"],
                created_by=admin.id,
                approved_by=admin.id,
                approved_at=utc_now(),
                status="active",
            )
            db.session.add(project)
            db.session.flush()
            created_project = True

        for index, (text, gold) in enumerate(SAMPLE_TASKS):
            exists = db.session.scalar(
                db.select(Task.id).where(
                    Task.project_id == project.id,
                    Task.input_data == text,
                )
            )
            if exists is not None:
                continue
            task = Task(
                project_id=project.id,
                input_data=text,
                expected_output=gold,
            )
            # Pre-assign the first four tasks so two annotators have a
            # live queue; the rest stay in the pending pool.
            if index < 4:
                assignee = annotators[index % 2]
                task.status = "assigned"
                task.assigned_to = assignee.id
                assigned_tasks += 1
            db.session.add(task)
            created_tasks += 1

        # Draft qualification quiz for the sample project (Phase 9):
        # visible in the admin quiz console, not gating until the admin
        # activates it deliberately.
        quiz_exists = db.session.scalar(
            db.select(QualificationQuiz.id).where(
                QualificationQuiz.project_id == project.id
            )
        )
        if quiz_exists is None:
            questions, errors = quizzes.parse_quiz_text(
                "Q: A task asks you to translate a Hausa sentence into"
                " English. What matters most?\n"
                "- Matching the sentence length\n"
                "* Preserving the meaning in natural English\n"
                "- Using formal academic language always\n"
                "Q: You are unsure about a task's instructions. What"
                " should you do?\n"
                "* Skip it and contact Brydl support\n"
                "- Guess and submit quickly\n"
                "- Copy another annotator's answer"
            )
            if not errors:
                db.session.add(QualificationQuiz(
                    project_id=project.id,
                    title="Hausa translation basics",
                    questions_json=quizzes.questions_payload(questions),
                    pass_score=80,
                    max_attempts=3,
                    status="draft",
                    created_by=admin.id,
                ))

        db.session.commit()
        log_event(
            "system.seed",
            actor_name="seed",
            detail={
                "created_users": created_users,
                "created_project": created_project,
                "created_tasks": created_tasks,
                "assigned_tasks": assigned_tasks,
            },
            commit=True,
        )

        print()
        print("Development data seeded (existing rows skipped):")
        print(f"  users created    : {created_users}")
        print(f"  project created  : {created_project}")
        tasks_line = (
            f"  tasks created    : {created_tasks}"
            f" ({assigned_tasks} pre-assigned)"
        )
        print(tasks_line)
        print()
        print("Sign-in credentials (DEVELOPMENT ONLY):")
        print(f"  admin      {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
        print(
            "  annotators annotator1@brydl.com … annotator5@brydl.com"
            f" / {ANNOTATOR_PASSWORD}"
        )
        print()
        print("Change the admin password before any real use.")


if __name__ == "__main__":
    main()
