"""RedLab — Dynamic Red-Teaming as a Service (v14).

This package holds all code for the RedLab product line: an async FastAPI
sub-application mounted at ``/redlab/v1`` that wraps the Flask dashboard
via :mod:`a2wsgi`.

Package layout (one module per Phase 0-9 plan):

* :mod:`redlab.flags`  — feature flag ``REDLAB_ENABLED`` (default off).
* :mod:`redlab.app`    — FastAPI sub-app, healthcheck, JSON error boundary.
* :mod:`redlab.crypto` — Fernet helpers for client API keys (Phase 2).
* :mod:`redlab.models` — SQLAlchemy models in the ``redlab`` Postgres
  schema (Phase 2).
* :mod:`redlab.ssrf`   — outbound URL allow-list / SSRF guard (Phase 3).
* :mod:`redlab.connector` — async ``httpx`` client to model endpoints
  (Phase 3).
* :mod:`redlab.mockmodel` — deterministic offline mock model (Phase 3).
* :mod:`redlab.auth`   — session-cookie / CSRF bridge for annotator chat
  (Phase 4).
* :mod:`redlab.events` — in-process event dispatcher for breach / conclude
  events (Phase 5).
* :mod:`redlab.integration` — the *only* module Flask code may import;
  bridges RedLab events back onto the submission/QA tables (Phase 5).

Import contract (enforced by ``import-linter`` in Phase 9):

* ``redlab.*`` MUST NOT import from Flask routes, templates, or core
  Flask business logic.
* Flask code MUST NOT import from ``redlab.*`` except
  :mod:`redlab.integration`.
"""

__all__ = ["__version__"]
__version__ = "14.0.0"
