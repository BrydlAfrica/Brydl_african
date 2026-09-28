"""Shared Flask extension instances for Brydl Dashboard.

Extensions are instantiated here, once, at module level — not inside
create_app(). models.py and the route modules import them from this module,
which keeps every import one-directional (module -> extensions) and prevents
the circular-import problems that occur when models import from app.py.
"""

from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Engine, event

db = SQLAlchemy()
login_manager = LoginManager()
# Alembic wrapper: initialised with the app in create_app(); the
# migrations/ package it drives is generated against this shared db.
migrate = Migrate()


@event.listens_for(Engine, "connect")
def _configure_sqlite_connection(dbapi_connection, connection_record):
    """Harden every new SQLite connection at the driver level.

    Two pragmas, executed on the raw DBAPI connection (outside any
    SQLAlchemy transaction, which SQLite requires for these to stick):

    - ``PRAGMA foreign_keys=ON``: SQLite silently ignores foreign keys by
      default. Without this, every ForeignKey() in models.py is
      decorative — dangling annotator_id / task_id / project_id values
      are accepted. PostgreSQL enforces FKs natively, so this pragma is
      what makes SQLite behave like our future production database.
    - ``PRAGMA journal_mode=WAL``: lets annotators keep reading while
      another request's write commits, instead of every reader blocking
      on the writer — important once several annotators work at once.

    The guard makes this a no-op for non-SQLite drivers (e.g. psycopg2),
    so the PostgreSQL migration needs no changes here.

    Args:
        dbapi_connection: A raw DBAPI connection just created by the pool.
        connection_record: SQLAlchemy's pool bookkeeping record (unused).
    """
    if type(dbapi_connection).__module__ != "sqlite3":
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()
