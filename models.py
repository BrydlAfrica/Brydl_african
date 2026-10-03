"""SQLAlchemy models for Brydl Dashboard.

Five tables: users, projects, tasks, submissions, payments.

Design notes:
- Every foreign key is paired with exactly one db.relationship() using
  backref, so related objects are reachable from both directions
  (e.g. task.project and project.tasks). Foreign-key *enforcement* in
  SQLite is switched on per-connection by extensions.py (PostgreSQL
  enforces it natively).
- The valid values for every status column are defined once as module-level
  tuples. They drive the database CheckConstraints here and drive the
  Flask-WTF SelectField choices in the forms — one source of truth, no
  drift.
- CheckConstraints also pin workflow *invariants*: a pending task cannot
  have an assignee (and vice versa), a reviewed submission must carry its
  review artifacts, a paid payment must record when and how it was paid.
- All datetime values are stored as naive UTC via utc_now(). This stores
  exactly what datetime.utcnow() stored, but without the DeprecationWarning
  Python 3.12+ raises for utcnow().
- Money is Decimal USD end to end: db.Numeric(10, 2) columns, passed
  through models.money() (quantize to 0.01, half-up) at every Python
  aggregation boundary. Floats are never used for money arithmetic.
- Payout tracking: an approved Submission carries a nullable payment_id
  claim. Unclaimed (NULL) means the work is still owed; a claim is set
  once, when a payout run bundles the work into a Payment, and is never
  overwritten — every submission is provably covered by exactly one
  payment. Naira figures are display-time conversions only (see
  routes.usd_to_ngn); the database stores USD exclusively.
"""

import logging
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from flask_login import UserMixin
from sqlalchemy import case, func, select, text
from werkzeug.security import check_password_hash, generate_password_hash

from config import TERMS_VERSION
from extensions import db

logger = logging.getLogger(__name__)

# --- Allowed state values (single source of truth) --------------------------
TASK_STATUSES = (
    "pending", "assigned", "in_progress", "submitted", "approved", "rejected",
)
SUBMISSION_STATUSES = ("submitted", "approved", "rejected", "revision")
PROJECT_STATUSES = ("draft", "pending_approval", "active", "paused", "completed", "rejected")
# Staff/annotator roles:
#   admin            = full control (money, projects, team, settings)
#   project_manager  = runs assigned projects (tasks, exports, draft
#                      quizzes, client links, team view); cannot touch
#                      payouts, secrets, staff, quiz gate state, or
#                      approve/reject submissions
#   qa               = submission review + quality scoring
#   auditor          = arbitration + audit trail
#   annotator        = the workspace (claim + do + submit work)
# Drives ck_users_role.
ROLES = ("admin", "project_manager", "qa", "auditor", "annotator")
# Staff roles that log into the management side of the app (not the
# annotator workspace) — used by the post-login dashboard router and
# the navbar rendering so new staff links auto-appear.
STAFF_ROLES = ("admin", "project_manager", "qa", "auditor")
# Feedback thread lifecycle:
#   new        = submitted, waiting on admin
#   reviewed   = admin has seen it, working on it
#   resolved   = closed (reply delivered)
FEEDBACK_STATUSES = ("new", "reviewed", "resolved")
PAYMENT_STATUSES = ("pending", "paid", "failed")
# SFT builder lifecycle: draft (being rewritten) -> ready (export
# candidate) -> exported (terminal + frozen; provenance must match the
# shipped corpus).
SFT_STATUSES = ("draft", "ready", "exported")
# Where a pair's harvest came from (consensus ruling > approved
# submission > gold key > typed by hand).
SFT_SOURCES = ("consensus", "approved", "gold", "manual")

# Labelling task taxonomy. Drives the project form's SelectField choices.
# Deliberately NOT enforced by a database constraint: this taxonomy grows
# faster than the schema, so it is validated at the form layer instead.
TASK_TYPES = (
    "classification",
    "translation",
    "ner",
    "rlhf",
    "preference",
    "audio_transcription",
    "image_labeling",
)

# Tasks assigned to an annotator that still need work before submission.
OPEN_TASK_STATUSES = ("assigned", "in_progress")


def utc_now() -> datetime:
    """Return the current UTC time as a naive datetime.

    Equivalent in stored value to datetime.utcnow(), which SQLite stores
    cleanly, but without the DeprecationWarning Python 3.12+ raises for
    utcnow().
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Money invariant: every monetary value in the system is a Decimal
# quantized to exactly 2 decimal places (USD cents). Columns use
# db.Numeric(10, 2); Python-side aggregation boundaries pass through
# money() so floats can never leak cent-level drift into the ledger.
TWO_PLACES = Decimal("0.01")


def money(value) -> Decimal:
    """Coerce any numeric value to a 2-decimal Decimal (USD cents).

    Floats are converted through repr() (the shortest string that
    round-trips), so 0.1 becomes exactly Decimal("0.10") instead of the
    binary artefact Decimal(0.1) would produce.

    Args:
        value: A Decimal, float, int, or numeric string.

    Returns:
        The value quantized to 2 decimal places (half-up).

    Raises:
        InvalidOperation: If the value cannot be interpreted as a number.
    """
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, float):
        candidate = Decimal(repr(value))
    else:
        candidate = Decimal(str(value))
    return candidate.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _in_constraint(column: str, values: tuple) -> str:
    """Build a SQL ``column IN (...)`` expression for a CheckConstraint.

    Args:
        column: SQL column name to test.
        values: Allowed string values.

    Returns:
        A SQL string such as ``status IN ('pending', 'assigned')``.
    """
    joined = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({joined})"


class User(UserMixin, db.Model):
    """A Brydl account: either an admin (CTO / QA) or an annotator."""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    # Email is always stored lowercase (the login form normalizes it
    # before lookup): SQLite uniqueness is case-sensitive, so
    # "Ada@Brydl.NG" and "ada@brydl.ng" would otherwise be two accounts.
    email = db.Column(
        db.String(255), unique=True, nullable=False, index=True
    )
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="annotator")
    # Deactivate instead of deleting: rows in tasks/submissions/payments
    # reference users.id and payroll history must survive an exit.
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    # Terms & Conditions consent: the version this account has accepted.
    # The DEFAULT is the currently published version, so scripted/seeded
    # accounts count as accepted; the Team page creates accounts with
    # terms_version="" (empty string = must accept) so real annotators
    # are sent to the terms page first (consent recorded in the audit
    # trail). Bumping config.TERMS_VERSION re-prompts everyone whose
    # stored version is older. ("" sentinel, not NULL: a Python-side
    # column default always replaces NULL at insert time.)
    terms_version = db.Column(
        db.String(32), nullable=False, default=TERMS_VERSION
    )
    terms_accepted_at = db.Column(db.DateTime, nullable=True)
    # UI colour theme ("light"/"dark"); None renders as light.
    theme = db.Column(db.String(10), nullable=False, default="light")
    # Opt-in mirror of in-app notifications to the user's email.
    # Default False — transactional mail (password reset) is always
    # sent; bell-feed notifications only email when the user opts in.
    email_notifications_opt_in = db.Column(
        db.Boolean, nullable=False, default=False, server_default=text("0")
    )
    # Self-service password reset: the SHA-256 hash of a one-off token
    # (never the token itself) plus its UTC expiry. Nullable = no reset
    # in flight; cleared the moment the token is consumed, so every
    # link is single-use.
    reset_token_hash = db.Column(db.String(64), nullable=True)
    reset_token_expires = db.Column(db.DateTime, nullable=True)
    # Payout account (annotator-supplied, admin-visible for payment
    # runs). Nullable as a unit: absent until the annotator fills it.
    # The number is stored as text (leading zeros are meaningful) and
    # validated as a 10-digit NUBAN at the form layer.
    bank_name = db.Column(db.String(120), nullable=True)
    # Fernet ciphertext (see security_crypto). Plaintext NEVER sits in
    # this column; last4 covers the masked UI without decryption.
    bank_account_number = db.Column(db.String(512), nullable=True)
    bank_account_last4 = db.Column(db.String(4), nullable=True)
    bank_account_name = db.Column(db.String(120), nullable=True)
    # "savings" or "current" (NULL until payout details are saved).
    bank_account_type = db.Column(db.String(10), nullable=True)
    # Cached Paystack transfer recipient (Tier 2 payouts): created on
    # demand from the stored bank details at the first Paystack payout
    # and reused afterwards. Nullable = never paid via Paystack yet.
    paystack_recipient_code = db.Column(
        db.String(100), nullable=True
    )
    # Optional contact number for payment questions (7-15 digits,
    # optional leading "+"; validated at the form layer). Never
    # written to the audit trail or payout CSVs.
    phone = db.Column(db.String(24), nullable=True)
    # Credential-change counter baked into the Flask-Login session id
    # (see get_id): bumping it instantly invalidates every existing
    # session AND remember-me cookie for the account.
    auth_version = db.Column(
        db.Integer, nullable=False, default=0, server_default="0"
    )

    __table_args__ = (
        # Enforce the two valid roles at the database level, not just in
        # form validation, so a bug can never write an unknown role.
        db.CheckConstraint(
            _in_constraint("role", ROLES), name="ck_users_role"
        ),
        # Annotator pickers and per-role dashboards filter on the pair.
        db.Index("ix_users_role_active", "role", "is_active"),
        # Password-reset links look users up by hashed token.
        db.Index("ix_users_reset_token_hash", "reset_token_hash"),
    )

    # Related objects exposed by backrefs defined on the other models:
    #   projects_created (Project.created_by)
    #   assigned_tasks   (Task.assigned_to)
    #   submissions      (Submission.annotator_id)
    #   payments         (Payment.annotator_id)

    def set_password(self, password: str) -> None:
        """Hash and store *password* using Werkzeug's defaults.

        Werkzeug 3.x uses scrypt with a per-user random salt by default,
        so no explicit method/rounds tuning is needed.

        Args:
            password: The plaintext password to hash. Never stored.
        """
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        """Return True if *password* matches the stored hash.

        Args:
            password: The plaintext password candidate.
        """
        return check_password_hash(self.password_hash, password)

    def get_pending_tasks_count(self) -> int:
        """Count this annotator's open tasks that still need work.

        "Open" means assigned to this user with status 'assigned' or
        'in_progress' — actionable work sitting in their queue. Note that
        status 'pending' is *not* counted: in the master workflow a pending
        task is unassigned work that no one owns yet.

        Returns:
            The number of open tasks, or 0 if the query fails (the error
            is logged; a dashboard stat should never raise a 500).
        """
        try:
            stmt = select(func.count(Task.id)).where(
                Task.assigned_to == self.id,
                Task.status.in_(OPEN_TASK_STATUSES),
            )
            return int(db.session.scalar(stmt) or 0)
        except Exception:
            logger.exception(
                "get_pending_tasks_count failed for user %s.", self.id
            )
            return 0

    def get_total_earnings(self) -> Decimal:
        """Total USD earned from QA-approved submissions.

        Earnings are computed from approved submissions (joined through
        their task to the project's pay_per_task), so the figure includes
        money earned but not yet paid out. The payments table, by contrast,
        records actual payouts — the difference between the two is the
        annotator's outstanding balance. Decimal end to end (no float
        money).

        Returns:
            Earnings in USD as a Decimal rounded to 2 places, or
            Decimal("0.00") on failure.
        """
        try:
            stmt = (
                select(func.coalesce(
                    func.sum(Project.pay_per_task), Decimal("0.00")
                ))
                .select_from(Submission)
                # Relationship joins: no hand-written ON clauses needed.
                .join(Submission.task)
                .join(Task.project)
                .where(
                    Submission.annotator_id == self.id,
                    Submission.status == "approved",
                )
            )
            return money(db.session.scalar(stmt))
        except Exception:
            logger.exception(
                "get_total_earnings failed for user %s.", self.id
            )
            return Decimal("0.00")

    def get_id(self) -> str:
        """Return the Flask-Login session identifier for this account.

        The id embeds ``auth_version`` ("id.version"). The user loader
        in app.py compares the version stored in the session cookie
        against the account's current counter, so the moment a password
        is changed or reset — by the user, by an admin, or via an
        emailed reset link — every previously issued session and
        remember-me cookie for the account stops authenticating.

        Returns:
            The "<primary_key>.<auth_version>" string.
        """
        return f"{self.id}.{self.auth_version}"

    def set_bank_account(self, number_plain: str) -> None:
        """Store a bank number encrypted, with its last4 for display.

        Args:
            number_plain: The validated 10-digit NUBAN.
        """
        from security_crypto import encrypt_bank_number

        self.bank_account_number = encrypt_bank_number(number_plain)
        self.bank_account_last4 = number_plain[-4:]

    def get_bank_account_number(self) -> str | None:
        """Return the decrypted bank number (legacy plaintext safe).

        Returns:
            The plaintext NUBAN, or None when not set.
        """
        from security_crypto import decrypt_bank_number

        return decrypt_bank_number(self.bank_account_number)

    def __repr__(self) -> str:
        """Return a debug representation that never exposes the hash."""
        return (
            f"<User id={self.id} email={self.email!r} "
            f"role={self.role!r} active={self.is_active}>"
        )


class Project(db.Model):
    """A labelling engagement for a client, bundling many tasks."""

    __tablename__ = "projects"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    client_name = db.Column(db.String(200), nullable=False)
    task_type = db.Column(db.String(50), nullable=False)
    pay_per_task = db.Column(db.Numeric(10, 2), nullable=True)  # USD; NULL until admin approves
    # Proposed rate set by a PM at create time. Admin can revise this
    # when approving; once approved pay_per_task is set and this is NULLed.
    proposed_pay_per_task = db.Column(db.Numeric(10, 2), nullable=True)
    # Optional note admin sends when rejecting a pending project (free text).
    approval_note = db.Column(db.Text, nullable=True)
    # Optional due date shown to admins and annotators (UTC day).
    deadline = db.Column(db.Date, nullable=True)
    # Consensus depth: how many annotators independently work each task
    # (1 = single annotation; 2-3 = RLHF consensus with arbitration).
    required_annotations = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    instructions = db.Column(db.Text, nullable=False)
    # Optional rubric dimensions (RLHF Phase 5), one per line —
    # parsed/validated by rubric.py; empty/NULL = rubric off. Storage
    # (not a child table): at most a handful of short labels per
    # project, read-only config for the annotator form.
    rubric_dimensions = db.Column(db.Text, nullable=True)
    # Default stays "active" so admin + existing tests create live
    # projects transparently; PM proposals explicitly set status='draft'
    # and pass through pending_approval before going live.
    status = db.Column(
        db.String(20), nullable=False, default="active", index=True
    )
    created_by = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    approved_by = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )
    approved_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    # project.creator -> User; user.projects_created -> list[Project]
    creator = db.relationship("User", foreign_keys=[created_by], backref="projects_created")
    approver = db.relationship("User", foreign_keys=[approved_by])

    __table_args__ = (
        # Consensus depth is a closed set: 1, 2 or 3 blind
        # annotators per task.
        db.CheckConstraint(
            "required_annotations IN (1, 2, 3)",
            name="ck_projects_consensus_depth",
        ),
        db.CheckConstraint(
            _in_constraint("status", PROJECT_STATUSES),
            name="ck_projects_status",
        ),
        # pay_per_task may be NULL while the project is in draft / pending
        # approval / rejected; once live it must be > 0.
        db.CheckConstraint(
            "pay_per_task IS NULL OR pay_per_task > 0",
            name="ck_projects_pay_per_task",
        ),
        db.CheckConstraint(
            "proposed_pay_per_task IS NULL OR proposed_pay_per_task > 0",
            name="ck_projects_proposed_pay",
        ),
    )

    def get_completion_percentage(self) -> float:
        """Percentage of this project's tasks that QA has approved.

        Completed means task status 'approved' — submitted-but-unreviewed
        work is intentionally excluded so the figure reflects verified
        progress, not optimistic progress.

        Returns:
            Completion as 0.0-100.0 rounded to one decimal. Returns 0.0
            for a project with no tasks yet (avoids ZeroDivisionError)
            or if the query fails (logged, never raises).
        """
        try:
            # Single round-trip: COUNT(*) plus a conditional sum of the
            # approved tasks, instead of two separate queries.
            stmt = select(
                func.count(Task.id),
                func.coalesce(
                    func.sum(case((Task.status == "approved", 1), else_=0)),
                    0,
                ),
            ).where(Task.project_id == self.id)
            total, approved = db.session.execute(stmt).one()
            if not total:
                return 0.0
            return round((approved / total) * 100.0, 1)
        except Exception:
            logger.exception(
                "get_completion_percentage failed for project %s.", self.id
            )
            return 0.0

    def __repr__(self) -> str:
        """Return a debug representation of the project."""
        return (
            f"<Project id={self.id} name={self.name!r} "
            f"type={self.task_type!r} status={self.status!r}>"
        )


class Task(db.Model):
    """A single unit of labelling work belonging to a project."""

    __tablename__ = "tasks"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True
    )
    input_data = db.Column(db.Text, nullable=False)
    # Gold answer where one exists (seed/gold tasks). Nullable: RLHF
    # preference data and open-ended tasks have no single correct output.
    # SECURITY: never send this field to annotator-facing routes/templates
    # — it is the answer key for QA scoring.
    expected_output = db.Column(db.Text, nullable=True)
    status = db.Column(
        db.String(20), nullable=False, default="pending", index=True
    )
    # Nullable: a pending task is unassigned work nobody owns yet.
    # UTC timestamp of a SELF-claim (Tier 1); admin assignment leaves
    # it NULL, so the daily claim cap only counts self-service work.
    claimed_at = db.Column(db.DateTime, nullable=True)
    # Sibling tasks of one consensus round share a group (NULL when the
    # project's required_annotations is 1).
    consensus_group_id = db.Column(
        db.Integer,
        db.ForeignKey("consensus_groups.id"),
        nullable=True,
        index=True,
    )
    assigned_to = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True, index=True
    )
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    # task.project -> Project; project.tasks -> list[Task]
    project = db.relationship("Project", backref="tasks")
    # task.assignee -> User; user.assigned_tasks -> list[Task]
    assignee = db.relationship("User", backref="assigned_tasks")

    __table_args__ = (
        db.CheckConstraint(
            _in_constraint("status", TASK_STATUSES), name="ck_tasks_status"
        ),
        # Workflow invariant: exactly the unowned tasks are 'pending'.
        # Both directions are enforced — a pending task with an assignee
        # and an owned task without one are both rejected by the DB.
        db.CheckConstraint(
            "(status = 'pending') = (assigned_to IS NULL)",
            name="ck_tasks_assignment",
        ),
        # Annotator queue: WHERE assigned_to=? AND status IN (...) ORDER
        # BY created_at — and every per-annotator open-task count.
        db.Index("ix_tasks_assignee_status", "assigned_to", "status"),
        # Admin task list + project progress: filter by project and
        # status (counts group by status within one project).
        db.Index("ix_tasks_project_status", "project_id", "status"),
    )

    def __repr__(self) -> str:
        """Return a debug representation of the task."""
        return (
            f"<Task id={self.id} project_id={self.project_id} "
            f"status={self.status!r} assigned_to={self.assigned_to}>"
        )


class ConsensusGroup(db.Model):
    """A blind-consensus round: N sibling tasks, one prompt.

    The group carries the arbitration state. Sibling tasks point at it
    via tasks.consensus_group_id; agreement/conflict is derived from
    the siblings' latest submissions (see consensus.py). A resolved
    group is frozen — the auditor's call is binding.
    """

    __tablename__ = "consensus_groups"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer,
        db.ForeignKey("projects.id"),
        nullable=False,
        index=True,
    )
    required_count = db.Column(db.Integer, nullable=False, default=2)
    status = db.Column(
        db.String(20),
        nullable=False,
        default="pending",
        index=True,
    )
    decided_choice = db.Column(db.String(100), nullable=True)
    decided_note = db.Column(db.Text, nullable=True)
    resolved_by = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )
    resolved_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=utc_now
    )

    __table_args__ = (
        # The arbitration lifecycle is a closed set; resolved is
        # terminal and frozen.
        db.CheckConstraint(
            "status IN ('pending', 'agreed', 'conflict', 'resolved')",
            name="ck_consensus_status",
        ),
    )

    tasks = db.relationship(
        "Task",
        backref="consensus_group",
        lazy="selectin",
    )
    project = db.relationship("Project", lazy="joined")

    def __repr__(self) -> str:
        """Return a debug representation of the group."""
        return (
            f"<ConsensusGroup id={self.id} project={self.project_id} "
            f"status={self.status!r}>"
        )


class SftExample(db.Model):
    """One supervised-fine-tuning training pair (RLHF Phase 4).

    Provenance: harvested from finalized work (a binding consensus
    ruling, an approved submission, or the seed gold key) and tied to
    its source task. ``source_completion`` freezes the machine-
    harvested starting text so any human rewrite of ``completion`` is
    auditable against it; a written ``edit_note`` is required whenever
    the two differ. ``prompt``/``completion`` are denormalized copies
    (the pair is the deliverable; the source rows stay untouched) and
    raw at rest — rendered only through the trusted-rendering path on
    admin surfaces.
    """

    __tablename__ = "sft_examples"

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(
        db.Integer, db.ForeignKey("tasks.id"), nullable=False, index=True
    )
    # Denormalized provenance (same pattern as ConsensusGroup) so the
    # builder list and export filter without a join.
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True
    )
    prompt = db.Column(db.Text, nullable=False)
    # The machine-harvested completion at creation time (immutable
    # audit anchor for the rewrite).
    source_completion = db.Column(db.Text, nullable=False)
    # The editable gold completion that exports carry.
    completion = db.Column(db.Text, nullable=False)
    # Optional system message carried into the export record.
    system_prompt = db.Column(db.Text, nullable=True)
    # Written explanation REQUIRED whenever completion deviates from
    # source_completion (deviation clause).
    edit_note = db.Column(db.Text, nullable=True)
    status = db.Column(
        db.String(20), nullable=False, default="draft", index=True
    )
    source = db.Column(db.String(20), nullable=False, default="manual")
    created_by = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False
    )
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    # sft_example.task -> Task; task.sft_examples -> list[SftExample]
    task = db.relationship(
        "Task", backref="sft_examples"
    )
    # sft_example.author -> User (admin who created the pair)
    author = db.relationship("User", backref="sft_examples")

    __table_args__ = (
        db.CheckConstraint(
            _in_constraint("status", SFT_STATUSES), name="ck_sft_status"
        ),
        db.CheckConstraint(
            _in_constraint("source", SFT_SOURCES), name="ck_sft_source"
        ),
    )

    def __repr__(self) -> str:
        """Return a debug representation of the example."""
        return (
            f"<SftExample id={self.id} task={self.task_id} "
            f"status={self.status!r} source={self.source!r}>"
        )


class ClientAccessToken(db.Model):
    """A scoped client-portal access token for one project (Phase 7).

    The portal lets a client follow their project's progress and
    download approved, client-safe deliverables WITHOUT an account.
    Only the sha256 hash of the opaque token is stored (32-byte
    urlsafe raw = unguessable), so a database leak never exposes live
    links. Deactivation is instant and audited; expiry is optional.
    """

    __tablename__ = "client_access_tokens"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True
    )
    # Who the link is for (e.g. the client company or contact).
    label = db.Column(db.String(120), nullable=False)
    # sha256 hex of the raw token — the raw value is shown exactly
    # once at creation and never stored.
    token_hash = db.Column(
        db.String(64), unique=True, nullable=False, index=True
    )
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_by = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False
    )
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    expires_at = db.Column(db.DateTime, nullable=True)
    last_used_at = db.Column(db.DateTime, nullable=True)
    last_used_ip = db.Column(db.String(64), nullable=True)

    # client_token.project -> Project
    project = db.relationship("Project", backref="client_tokens")
    # client_token.creator -> User
    creator = db.relationship("User")

    def __repr__(self) -> str:
        """Return a debug representation that never exposes tokens."""
        return (
            f"<ClientAccessToken id={self.id} "
            f"project={self.project_id} active={self.is_active}>"
        )


# Media workspace kinds (Phase 8). Content types are allowlisted at
# upload AND sniffed by magic bytes; SVG is excluded by design.
class ApiToken(db.Model):
    """One project-scoped machine API key (Phase 10, API tier).

    The raw key (``brydl_`` + 32 urlsafe bytes) is shown to the admin
    exactly once at issue time; only its sha256 is stored — the same
    temp-password contract as client-portal links. Requests carry the
    raw key in the Authorization header (Bearer); there is no cookie
    or session path, so CSRF does not apply by construction. A key is
    confined to its project: every handler re-checks the project id
    against the key, and unknown projects are indistinguishable 404s.
    """

    __tablename__ = "api_tokens"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer,
        db.ForeignKey("projects.id"),
        nullable=False,
        index=True,
    )
    label = db.Column(db.String(120), nullable=False)
    key_hash = db.Column(
        db.String(64), nullable=False, unique=True, index=True
    )
    # Short display prefix so staff can tell keys apart without the
    # raw secret ("brydl_aB3dEf01…").
    key_prefix = db.Column(db.String(16), nullable=False)
    created_by = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False
    )
    created_at = db.Column(
        db.DateTime, nullable=False, default=utc_now
    )
    last_used_at = db.Column(db.DateTime, nullable=True)
    request_count = db.Column(
        db.Integer, nullable=False, default=0
    )
    revoked_at = db.Column(db.DateTime, nullable=True)

    project = db.relationship("Project", backref="api_tokens")

    def is_active(self) -> bool:
        """Return True while the key is neither revoked nor expired."""
        return self.revoked_at is None

    def __repr__(self) -> str:
        """Return a debug representation that never carries the key."""
        return (
            f"<ApiToken id={self.id} project={self.project_id} "
            f"prefix={self.key_prefix!r} "
            f"revoked={self.revoked_at is not None}>"
        )


MEDIA_IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp",
                     "image/gif")
MEDIA_AUDIO_TYPES = ("audio/mpeg", "audio/wav", "audio/ogg",
                     "audio/mp4")
MEDIA_KINDS = {"image": MEDIA_IMAGE_TYPES, "audio": MEDIA_AUDIO_TYPES}


# Qualification quizzes (Phase 9): the per-project gate between
# sign-up and self-serve work. Quiz statuses are a closed lifecycle;
# attempts record every graded try (exemptions are admin-granted
# passes with no answer sheet).
QUIZ_STATUSES = ("draft", "active", "retired")
QUIZ_ATTEMPT_STATUSES = ("passed", "failed", "exempt")


class QualificationQuiz(db.Model):
    """One multiple-choice qualification quiz for a project.

    A project carries at most one quiz (unique project_id). While it
    is 'active', only annotators with a passed (or admin-exempt)
    attempt may claim, be assigned, or open its tasks. Questions live
    as a canonical JSON payload authored in the admin paste format
    (see quizzes.parse_quiz_text); correct-answer flags never leave
    the server.

    Lifecycle: draft (editable, not gating) -> active (gating) ->
    retired (not gating, history kept; can be re-drafted and edited).
    """

    __tablename__ = "qualification_quizzes"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer,
        db.ForeignKey("projects.id"),
        nullable=False,
        unique=True,
    )
    title = db.Column(db.String(200), nullable=False)
    questions_json = db.Column(db.Text, nullable=False)
    # Percent (1-100) of correct answers required to pass.
    pass_score = db.Column(db.Integer, nullable=False, default=80)
    # Graded tries allowed before the annotator is locked out.
    max_attempts = db.Column(db.Integer, nullable=False, default=3)
    status = db.Column(
        db.String(20), nullable=False, default="draft", index=True
    )
    created_by = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False
    )
    created_at = db.Column(
        db.DateTime, nullable=False, default=utc_now
    )
    updated_at = db.Column(
        db.DateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    project = db.relationship("Project", backref="qualification_quiz")

    __table_args__ = (
        db.CheckConstraint(
            "status IN ('draft', 'active', 'retired')",
            name="ck_quiz_status",
        ),
        db.CheckConstraint(
            "pass_score BETWEEN 1 AND 100", name="ck_quiz_score"
        ),
        db.CheckConstraint(
            "max_attempts BETWEEN 1 AND 20", name="ck_quiz_max_attempts"
        ),
        # Admin console: which projects are gated right now.
        db.Index(
            "ix_qualification_quizzes_project_status",
            "project_id",
            "status",
        ),
    )

    def __repr__(self) -> str:
        """Return a debug representation without the payload."""
        return (
            f"<QualificationQuiz id={self.id} project={self.project_id} "
            f"status={self.status!r}>"
        )


class QuizAttempt(db.Model):
    """One graded try (or admin-granted exemption) at a quiz.

    'passed' and 'exempt' both qualify the annotator for the quiz's
    project; 'failed' rows are kept as the honest history that backs
    the max_attempts counter. Exemptions carry no answer sheet —
    granted_by records the admin who vouched.
    """

    __tablename__ = "quiz_attempts"

    id = db.Column(db.Integer, primary_key=True)
    quiz_id = db.Column(
        db.Integer,
        db.ForeignKey("qualification_quizzes.id"),
        nullable=False,
        index=True,
    )
    annotator_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    status = db.Column(db.String(20), nullable=False)
    correct_count = db.Column(db.Integer, nullable=True)
    total_count = db.Column(db.Integer, nullable=True)
    score_percent = db.Column(db.Integer, nullable=True)
    # Canonical {"q_<index>": <option_index>} sheet (exempt: None).
    answers_json = db.Column(db.Text, nullable=True)
    granted_by = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )
    created_at = db.Column(
        db.DateTime, nullable=False, default=utc_now, index=True
    )

    __table_args__ = (
        db.CheckConstraint(
            "status IN ('passed', 'failed', 'exempt')",
            name="ck_quiz_attempt_status",
        ),
        db.CheckConstraint(
            "score_percent IS NULL OR score_percent BETWEEN 0 AND 100",
            name="ck_quiz_attempt_score",
        ),
        # The gate check and the attempts-left counter both resolve
        # through (annotator, quiz, status) in one index seek.
        db.Index(
            "ix_quiz_attempts_annotator_quiz",
            "annotator_id",
            "quiz_id",
            "status",
        ),
    )

    def __repr__(self) -> str:
        """Return a debug representation without the answer sheet."""
        return (
            f"<QuizAttempt id={self.id} quiz={self.quiz_id} "
            f"annotator={self.annotator_id} status={self.status!r}>"
        )


class MediaAsset(db.Model):
    """A piece of task media (image or audio) for one project (Phase 8).

    Bytes live in the database column (deviation, documented: $0
    storage, included in pg_dump backups, survives redeploys; a later
    object-store move only adds columns). Uploads are capped, typed
    and sniffed; identical re-uploads within a project reuse the
    first row (unique project+sha256). Tasks reference an asset via
    their input_data document's media_id — this table never points
    at tasks, so imports stay free to reference any prior upload.
    """

    __tablename__ = "media_assets"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True
    )
    kind = db.Column(db.String(10), nullable=False)  # image | audio
    content_type = db.Column(db.String(100), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    byte_size = db.Column(db.Integer, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    data = db.Column(db.LargeBinary, nullable=False)
    uploaded_by = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False
    )
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    __table_args__ = (
        db.CheckConstraint(
            "kind IN ('image', 'audio', 'document', 'other')", name="ck_media_kind"
        ),
        db.CheckConstraint(
            "byte_size > 0", name="ck_media_size"
        ),
        # Dedupe: one row per identical file per project (the upload
        # route reuses the existing asset instead of storing twice).
        db.Index(
            "ix_media_assets_project_sha",
            "project_id",
            "sha256",
            unique=True,
        ),
        db.Index("ix_media_project_kind", "project_id", "kind"),
    )

    def __repr__(self) -> str:
        """Return a debug representation that never carries bytes."""
        return (
            f"<MediaAsset id={self.id} project={self.project_id} "
            f"kind={self.kind!r} size={self.byte_size}>"
        )


class Submission(db.Model):
    """An annotator's answer to a task, plus the QA verdict on it.

    History note: the row's status moves submitted -> approved/rejected/
    revision. Whether a revision is resubmitted by updating this row or by
    inserting a new one (full audit trail) is decided when the task
    completion feature is built; the schema supports both. Only after that
    decision can a UNIQUE(task_id, annotator_id) constraint be considered.
    """

    __tablename__ = "submissions"

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(
        db.Integer, db.ForeignKey("tasks.id"), nullable=False, index=True
    )
    annotator_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    output_data = db.Column(db.Text, nullable=False)
    # 0-100, set by QA at review time. Null until then; the check
    # constraints keep bad scores and unreviewed verdicts out of the
    # database even if a form validator is ever bypassed.
    qa_score = db.Column(db.Float, nullable=True)
    qa_notes = db.Column(db.Text, nullable=True)
    status = db.Column(
        db.String(20), nullable=False, default="submitted", index=True
    )
    submitted_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    # Payout claim: set when the approved work is bundled into a Payment
    # by the payments feature. NULL means approved but not yet included
    # in any payout run. Claims are permanent (no unlink) so every
    # submission is provably covered by exactly one payment.
    payment_id = db.Column(
        db.Integer, db.ForeignKey("payments.id"), nullable=True, index=True
    )

    # submission.task -> Task; task.submissions -> list[Submission]
    task = db.relationship("Task", backref="submissions")
    # submission.annotator -> User; user.submissions -> list[Submission]
    annotator = db.relationship("User", backref="submissions")
    # submission.payment -> Payment; payment.submissions -> list[Submission]
    payment = db.relationship("Payment", backref="submissions")

    __table_args__ = (
        db.CheckConstraint(
            _in_constraint("status", SUBMISSION_STATUSES),
            name="ck_submissions_status",
        ),
        db.CheckConstraint(
            "qa_score IS NULL OR (qa_score >= 0 AND qa_score <= 100)",
            name="ck_submissions_qa_score",
        ),
        # Review integrity: a final verdict (approved/rejected) must carry
        # both the review timestamp and a score. 'revision' is exempt
        # because its resubmission flow (update vs new row) is not
        # decided yet.
        db.CheckConstraint(
            "status IN ('submitted', 'revision') OR"
            " (reviewed_at IS NOT NULL AND qa_score IS NOT NULL)",
            name="ck_submissions_review_integrity",
        ),
        # Personal tally + earnings: WHERE annotator_id=? AND status=?.
        db.Index(
            "ix_submissions_annotator_status", "annotator_id", "status"
        ),
        # Review queue + payout aggregation: the two hottest admin
        # queries filter on (status, ...) — submitted for QA, approved
        # and unclaimed for payments.
        db.Index("ix_submissions_status_payment", "status", "payment_id"),
        # History pages: newest-first per annotator.
        db.Index(
            "ix_submissions_annotator_submitted",
            "annotator_id",
            "submitted_at",
        ),
    )

    def __repr__(self) -> str:
        """Return a debug representation of the submission."""
        return (
            f"<Submission id={self.id} task_id={self.task_id} "
            f"annotator_id={self.annotator_id} status={self.status!r}>"
        )


class Payment(db.Model):
    """A payout of an annotator's earned balance for a billing period."""

    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    annotator_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    # USD, exact to the cent; Naira figures are display-time only.
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    currency = db.Column(db.String(3), nullable=False, default="USD")
    period_start = db.Column(db.DateTime, nullable=False)
    period_end = db.Column(db.DateTime, nullable=False)
    tasks_count = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(
        db.String(20), nullable=False, default="pending", index=True
    )
    # Chosen when the payment is executed (e.g. bank transfer), so it
    # stays null while the payment is merely pending.
    payment_method = db.Column(db.String(50), nullable=True)
    paid_at = db.Column(db.DateTime, nullable=True)
    # Paystack Transfers (Tier 2): the idempotency reference of the
    # initiated transfer; the webhook settles the row by looking it up.
    paystack_reference = db.Column(
        db.String(100), nullable=True, index=True
    )
    created_at = db.Column(
        db.DateTime, nullable=False, default=utc_now
    )

    # payment.annotator -> User; user.payments -> list[Payment]
    annotator = db.relationship("User", backref="payments")

    __table_args__ = (
        db.CheckConstraint(
            _in_constraint("status", PAYMENT_STATUSES),
            name="ck_payments_status",
        ),
        db.CheckConstraint("amount >= 0", name="ck_payments_amount"),
        db.CheckConstraint(
            "tasks_count >= 0", name="ck_payments_tasks_count"
        ),
        # Audit integrity: 'paid' means actually paid — the row must say
        # when (paid_at), and nothing else may claim a paid_at.
        db.CheckConstraint(
            "(status = 'paid') = (paid_at IS NOT NULL)",
            name="ck_payments_paid_at_pairing",
        ),
        # A paid payment must record the channel it went out through.
        db.CheckConstraint(
            "status <> 'paid' OR payment_method IS NOT NULL",
            name="ck_payments_method_when_paid",
        ),
        # Payments dashboard: pending payments per annotator, plus the
        # paid-this-month total (status, paid_at).
        db.Index("ix_payments_annotator_status", "annotator_id", "status"),
        db.Index("ix_payments_status_paid_at", "status", "paid_at"),
    )

    def __repr__(self) -> str:
        """Return a debug representation of the payment."""
        return (
            f"<Payment id={self.id} annotator_id={self.annotator_id} "
            f"amount={self.amount} {self.currency} "
            f"status={self.status!r}>"
        )


class AuditLog(db.Model):
    """Immutable, append-only record of one significant system action.

    Written by audit.log_event() and browsed by admins at
    /admin/audit. Nothing in the application ever updates or deletes
    these rows; the table is the system's source of truth for "who did
    what, when, from where".

    Columns:
        user_id: The acting user, when authenticated (nullable — failed
            anonymous sign-in attempts are audited too).
        actor_name: Display snapshot of the actor so the trail stays
            readable even without joins (and labels anonymous attempts
            with the email that was typed).
        action: Dotted event name ("review.verdict", "task.upload", ...).
        entity_type / entity_id: Polymorphic pointer at the target.
        detail: Small JSON payload with action-specific fields.
        ip_address / user_agent: Request origin (UA truncated to 200).
        created_at: UTC timestamp (indexed — the trail is read newest
            first).
    """

    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True, index=True
    )
    actor_name = db.Column(db.String(120), nullable=False, default="system")
    action = db.Column(db.String(100), nullable=False, index=True)
    entity_type = db.Column(db.String(50), nullable=True)
    entity_id = db.Column(db.Integer, nullable=True)
    detail = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(200), nullable=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=utc_now, index=True
    )

    __table_args__ = (
        db.CheckConstraint(
            "length(action) > 0", name="ck_audit_logs_action"
        ),
    )

    def __repr__(self) -> str:
        """Return a debug representation of the audit entry."""
        return (
            f"<AuditLog id={self.id} action={self.action!r} "
            f"actor={self.actor_name!r} "
            f"entity={self.entity_type}#{self.entity_id} "
            f"at={self.created_at:%Y-%m-%d %H:%M:%S}>"
        )


class Notification(db.Model):
    """One in-app notification for an annotator (bell icon feed).

    Created for the events an annotator must not miss while offline:
    tasks assigned, QA rejections, payouts sent. Optional payload is a
    small JSON blob; read_at marks when the annotator opened the feed.
    Emails ride the same event via notifications.notify() — this table
    is the durable in-app copy, never PII beyond what the annotator
    already sees about their own work.
    """

    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    kind = db.Column(db.String(40), nullable=False)
    payload = db.Column(db.Text, nullable=True)
    read_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=utc_now, index=True
    )

    __table_args__ = (
        # The badge query: unread items for one user, newest first.
        db.Index("ix_notifications_user_read", "user_id", "read_at"),
        db.CheckConstraint(
            "length(kind) > 0", name="ck_notifications_kind"
        ),
    )

    def __repr__(self) -> str:
        """Return a debug representation of the notification."""
        return (
            f"<Notification id={self.id} user={self.user_id} "
            f"kind={self.kind!r} read={self.read_at is not None}>"
        )


class ProjectAssignment(db.Model):
    """Grants a project_manager (or QA/auditor) access to a specific project.

    Admin assigns PMs explicitly (per the agreed PM-scope model): a PM
    sees only projects where they have an assignment row. The same table
    is reserved for future project-scoped QA/auditor grants too (a QA
    reviewer restricted to one vertical, etc.).
    """

    __tablename__ = "project_assignments"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    # role snapshot: redundant with users.role but guards against role
    # changes leaving stale assignment entries with no effect.
    role_in_project = db.Column(
        db.String(20),
        nullable=False,
        default="project_manager",
        server_default="project_manager",
    )
    assigned_by = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False
    )
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    project = db.relationship("Project", backref="assignments")
    user = db.relationship(
        "User",
        foreign_keys=[user_id],
        backref=db.backref("project_assignments", cascade="all, delete-orphan"),
    )
    assigner = db.relationship("User", foreign_keys=[assigned_by])

    __table_args__ = (
        db.UniqueConstraint(
            "project_id", "user_id", name="uq_assignments_project_user"
        ),
        db.Index("ix_assignments_user_role", "user_id", "role_in_project"),
        db.Index("ix_assignments_project_role", "project_id", "role_in_project"),
    )

    def __repr__(self) -> str:
        return (
            f"<ProjectAssignment project={self.project_id} user={self.user_id} "
            f"role={self.role_in_project!r}>"
        )


class SiteSetting(db.Model):
    """Editable operational setting stored in the DB.

    Secrets (SECRET_KEY, SMTP, Paystack, DB URL, BANK_ENC_KEY) stay in
    Render environment variables and never appear here. Every other
    operational knob — USD→NGN rate, pagination, rate limits, claim
    caps — lives here as a key/value row so admin can tweak them from
    /admin/settings without redeploying. Value is text; callers cast
    (Decimal/int/bool) after reading.
    """

    __tablename__ = "site_settings"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text, nullable=True)
    description = db.Column(db.String(255), nullable=True)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    def __repr__(self) -> str:
        return f"<SiteSetting {self.key}={self.value!r}>"


class FeedbackThread(db.Model):
    """A staff/annotator feedback thread or admin broadcast.

    Scope model (option C, in-app only):
      - Any authenticated user can open a thread (kind='feedback');
        admin sees all open threads in /admin/messages and can reply.
      - Admin can post a broadcast (kind='broadcast'); every active
        user sees it on their dashboard bell and messages page.
    No peer-to-peer DMs; no email delivery.
    """

    __tablename__ = "feedback_threads"

    id = db.Column(db.Integer, primary_key=True)
    # 'feedback'  = user -> admin suggestion/question
    # 'broadcast' = admin -> everyone announcement
    kind = db.Column(
        db.String(20), nullable=False, default="feedback", server_default="feedback"
    )
    subject = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    author_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    # For feedback threads: new / reviewed / resolved. Broadcasts are
    # always 'new' at author time and don't transition.
    status = db.Column(
        db.String(20), nullable=False, default="new", server_default="new", index=True
    )
    # For broadcasts: when set, the notification bell shows it until
    # read; feedback threads are also shown to their author.
    created_at = db.Column(
        db.DateTime, nullable=False, default=utc_now, index=True
    )
    resolved_at = db.Column(db.DateTime, nullable=True)

    author = db.relationship("User", backref="authored_feedback")
    replies = db.relationship(
        "FeedbackReply",
        backref="thread",
        cascade="all, delete-orphan",
        order_by="FeedbackReply.created_at",
    )

    __table_args__ = (
        db.CheckConstraint(
            _in_constraint("kind", ("feedback", "broadcast")),
            name="ck_feedback_kind",
        ),
        db.CheckConstraint(
            _in_constraint("status", FEEDBACK_STATUSES),
            name="ck_feedback_status",
        ),
        db.Index("ix_feedback_threads_kind_created", "kind", "created_at"),
        db.Index("ix_feedback_threads_author_created", "author_id", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<FeedbackThread id={self.id} kind={self.kind!r} "
            f"status={self.status!r} subject={self.subject!r}>"
        )


class FeedbackReply(db.Model):
    """One reply inside a feedback thread (admin or author)."""

    __tablename__ = "feedback_replies"

    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(
        db.Integer,
        db.ForeignKey("feedback_threads.id"),
        nullable=False,
        index=True,
    )
    author_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False
    )
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(
        db.DateTime, nullable=False, default=utc_now, index=True
    )

    author = db.relationship("User")

    def __repr__(self) -> str:
        return f"<FeedbackReply thread={self.thread_id} by={self.author_id}>"


class BackgroundJob(db.Model):
    """One unit of deferred work in the DB-backed queue (Phase 10).

    Durable by design: rows survive redeploys, need no second
    infrastructure credential, and ride every backup. The runner
    claims rows with a conditional UPDATE (status queued -> running)
    so any number of gunicorn workers — or a future dedicated worker
    service — can poll the same table without double execution.
    Payloads carry identifiers, never secrets: no bank data, no raw
    tokens, no Fernet material ever enters this table.
    """

    __tablename__ = "background_jobs"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(50), nullable=False)
    payload = db.Column(db.Text, nullable=True)  # small JSON object
    status = db.Column(
        db.String(20), nullable=False, default="queued", index=True
    )
    attempts = db.Column(db.Integer, nullable=False, default=0)
    max_attempts = db.Column(db.Integer, nullable=False, default=5)
    run_after = db.Column(
        db.DateTime, nullable=False, default=utc_now
    )
    last_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=utc_now
    )
    updated_at = db.Column(
        db.DateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    __table_args__ = (
        db.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed')",
            name="ck_jobs_status",
        ),
        db.CheckConstraint(
            "max_attempts BETWEEN 1 AND 20", name="ck_jobs_max_attempts"
        ),
        # The poller's single index seek: due, queued work in order.
        db.Index(
            "ix_background_jobs_status_run_after",
            "status",
            "run_after",
        ),
    )

    def __repr__(self) -> str:
        """Return a debug representation without the payload."""
        return (
            f"<BackgroundJob id={self.id} kind={self.kind!r} "
            f"status={self.status!r} attempts={self.attempts}>"
        )
