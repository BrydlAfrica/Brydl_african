"""RedLab feature flag.

Per the v14 architecture lock, the entire ``/redlab/*`` surface is gated
behind the ``REDLAB_ENABLED`` environment variable — it MUST be ``"true"``
(lowercase, exactly) for any RedLab route to accept traffic. Until Phase 9
(flag rehearsal + hardening) the default is OFF: importing this package
is safe, the FastAPI sub-app mounts but the ``/redlab/v1/*`` namespace
returns 404 with a JSON ``{ "error": "redlab_disabled" }`` body, and the
``/redlab/healthz`` endpoint is the only reachable route (so the
production health-check can switch over before flag-on day).

This module is import-safe even when FastAPI/httpx are not installed —
callers that only need to know the flag state will not trigger those
imports.
"""
from __future__ import annotations

import os


def is_redlab_enabled() -> bool:
    """Return ``True`` when ``REDLAB_ENABLED`` is (case-insensitively)
    ``"true"`` (whitespace trimmed).

    Other "truthy" values — ``"1"``, ``"yes"``, ``"on"``, any integer —
    are all treated as OFF. The narrow match avoids a half-on state
    from a mistyped env var; operators must explicitly set it to
    ``"true"`` (any casing) to expose ``/redlab/v1/*`` traffic.
    """
    return os.environ.get("REDLAB_ENABLED", "").strip().lower() == "true"
