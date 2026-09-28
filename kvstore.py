"""Optional Redis access for shared counters (Phase 10).

Redis is an accelerator, never a dependency: when REDIS_URL is unset
(or the server is unreachable) every caller falls back to an
in-process implementation and the app keeps working — a gunicorn
deployment with N workers then simply keeps N independent counters
(the documented limitation of the $0 path). Used today by the API
rate limiter; cache/session storage can ride the same lazy client
later without new plumbing.

The client is imported lazily so a missing redis package can never
break boot, and every call is failure-isolated: a dead Redis must
degrade to the in-process path, never raise into a request.
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_client = None
_checked_at = 0.0
# Re-probe a dead/unconfigured Redis at most this often (seconds) so
# a transient outage heals itself without a request-time penalty.
_PROBE_SECONDS = 60

# In-process fallback counters: {name: (window_epoch, count)}.
_local_counters: dict = {}


def get_redis():
    """Return a shared Redis client, or None when unavailable.

    The client is created once per process from REDIS_URL and pinged
    lazily; a failed probe is retried at most once per
    _PROBE_SECONDS. Never raises.

    Returns:
        A redis.Redis instance, or None.
    """
    global _client, _checked_at
    import flask

    url = flask.current_app.config.get("REDIS_URL", "")
    if not url:
        return None
    now = time.monotonic()
    if _client is not None and now - _checked_at < _PROBE_SECONDS:
        return _client
    with _lock:
        now = time.monotonic()
        if _client is not None and now - _checked_at < _PROBE_SECONDS:
            return _client
        try:
            import redis as redis_module

            candidate = redis_module.Redis.from_url(
                url,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            candidate.ping()
            _client = candidate
            logger.info("Redis connected for shared counters.")
        except Exception:
            logger.warning(
                "Redis unavailable (%s…) — using in-process"
                " counters.", url[:24],
            )
            _client = None
        _checked_at = now
    return _client


def bump_window(name: str, window_seconds: int) -> int:
    """Increment a named counter for the current fixed window.

    Args:
        name: Counter key (e.g. "rl:12:1700000060").
        window_seconds: Window length; counts reset each window.

    Returns:
        The 1-based count within this window.
    """
    client = get_redis()
    if client is not None:
        try:
            pipe = client.pipeline()
            pipe.incr(name, 1)
            pipe.expire(name, window_seconds + 1)
            return int(pipe.execute()[0])
        except Exception:
            logger.exception("Redis counter failed; falling back.")
    window = int(time.time()) // window_seconds
    with _lock:
        current = _local_counters.get(name)
        if current is None or current[0] != window:
            _local_counters[name] = (window, 1)
            return 1
        _local_counters[name] = (window, current[1] + 1)
        return current[1] + 1
