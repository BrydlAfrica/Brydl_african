"""SSRF defense for RedLab outbound model calls.

Before any client-private API key is sent to a ``Target.endpoint``, we
validate the URL and resolve its hostname, rejecting any address that
would route to an internal network, the cloud metadata service, or the
loopback interface (except in local-dev mode, where ``http://localhost``
is allowed so `mockmodel` / integration tests can run).

Design notes
------------
* **Hostname resolution happens inside the validator and is pinned.**
  We resolve with ``socket.getaddrinfo`` and reject if *any* returned
  address falls in a forbidden range. To prevent DNS-rebinding (TOCTOU)
  attacks, the validated IP is returned so the connector can pin the
  TCP connection directly to the exact IP that passed validation.
* **IPv4-mapped IPv6 normalization.** Addresses such as
  ``::ffff:127.0.0.1`` and ``::ffff:169.254.169.254`` are unwrapped to
  their IPv4 representations before CIDR evaluation.
* **Only https:// is accepted in production.** Plaintext http is
  allowed only when the host is loopback *and* the env var
  ``REDLAB_ALLOW_HTTP_LOCAL=1`` is set (test/dev mode).
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Forbidden CIDR ranges (any IPv4/IPv6 in this set → blocked).
_BLOCKED_NETS = [
    # IPv4 0.0.0.0/8 (covers 0.0.0.0 default route)
    ipaddress.ip_network("0.0.0.0/8"),
    # IPv4 loopback (127.0.0.0/8)
    ipaddress.ip_network("127.0.0.0/8"),
    # RFC 1918 private ranges
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    # Link-local / cloud-metadata (169.254.0.0/16) — this is the
    # GCP/AWS/Azure metadata-services block. Block unconditionally.
    ipaddress.ip_network("169.254.0.0/16"),
    # Carrier-grade NAT (RFC 6598) — not strictly private but not
    # publicly routable to an LLM API either; conservative block.
    ipaddress.ip_network("100.64.0.0/10"),
    # Limited broadcast / reserved
    ipaddress.ip_network("255.255.255.255/32"),
    # IPv4 multicast
    ipaddress.ip_network("224.0.0.0/4"),
    # IPv6 unspecified (::/128)
    ipaddress.ip_network("::/128"),
    # IPv6 loopback (::1/128)
    ipaddress.ip_network("::1/128"),
    # IPv6 unique-local (fc00::/7) — equivalent of RFC1918.
    ipaddress.ip_network("fc00::/7"),
    # IPv6 link-local (fe80::/10)
    ipaddress.ip_network("fe80::/10"),
    # IPv6 multicast (ff00::/8)
    ipaddress.ip_network("ff00::/8"),
]


_warned_http_local: bool = False


def _http_local_allowed() -> bool:
    """Return True if local http loopback is permitted this call."""
    global _warned_http_local
    allowed = os.environ.get(
        "REDLAB_ALLOW_HTTP_LOCAL", ""
    ).strip().lower() in ("1", "true", "yes")
    if allowed and not _warned_http_local:
        _warned_http_local = True
        logger.warning(
            "⚠️ BOOT/STARTUP WARNING: REDLAB_ALLOW_HTTP_LOCAL is enabled! "
            "Plaintext HTTP loopback is permitted. Do NOT use this flag in production!"
        )
    return allowed


def _is_blocked_ip(ip_str: str) -> bool:
    """Return True if the IP string falls in any blocked CIDR or property."""
    if not ip_str or not isinstance(ip_str, str):
        return True
    try:
        ip = ipaddress.ip_address(ip_str.strip())
    except ValueError:
        return True  # unparseable address — treat as blocked

    # Normalise IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1 -> 127.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if (ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or ip.is_reserved):
        return True

    for net in _BLOCKED_NETS:
        if ip.version == net.version and ip in net:
            return True
    return False


def resolve_and_validate_endpoint_url(
    url: str,
) -> tuple[bool, str | None, str | None]:
    """Validate endpoint URL and resolve its IP for connection pinning.

    Args:
        url: The full endpoint URL.

    Returns:
        ``(True, resolved_ip, None)`` on success.
        ``(False, None, reason)`` on failure.
    """
    if not url or not isinstance(url, str):
        return False, None, "invalid_url"

    try:
        parsed = urlparse(url.strip())
    except (ValueError, Exception):
        return False, None, "invalid_url"

    if not parsed.scheme or not parsed.hostname:
        return False, None, "invalid_url"

    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()

    if scheme == "https":
        pass  # ok
    elif scheme == "http":
        if not _http_local_allowed():
            return False, None, "scheme_not_https"
        # http only allowed for explicit loopback hosts in dev/test mode.
        if host in ("localhost", "127.0.0.1", "::1"):
            return True, "127.0.0.1", None
        return False, None, "scheme_not_https"
    else:
        return False, None, "scheme_not_https"

    # Hostnames that are IP-literal strings — check directly.
    try:
        ip = ipaddress.ip_address(host)
        if _is_blocked_ip(str(ip)):
            return False, None, "hostname_blocked"
        return True, str(ip), None
    except ValueError:
        pass  # hostname (not a literal IP) — fall through to DNS

    # Resolve via getaddrinfo; reject if ANY returned address is blocked.
    port = parsed.port or (443 if scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(
            host, port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except (socket.gaierror, UnicodeError, ValueError, Exception):
        logger.warning("RedLab SSRF: DNS resolution failed for %s", host)
        return False, None, "dns_resolution_failed"

    if not infos:
        return False, None, "dns_resolution_failed"

    first_ip = None
    for family, _type, _proto, _canonname, sockaddr in infos:
        ip_str = sockaddr[0]
        if _is_blocked_ip(ip_str):
            logger.warning(
                "RedLab SSRF: host %s resolves to blocked IP %s",
                host, ip_str,
            )
            return False, None, "hostname_blocked"
        if first_ip is None:
            first_ip = ip_str

    return True, first_ip, None


def validate_endpoint_url(url: str) -> tuple[bool, str | None]:
    """Validate an outbound LLM endpoint URL against SSRF rules.

    Legacy signature returning ``(ok, reason)``.
    """
    ok, _ip, reason = resolve_and_validate_endpoint_url(url)
    return ok, reason


def validate_target_url(
    url: str,
) -> tuple[bool, str | None, str | None]:
    """Validate a target endpoint URL against SSRF rules and resolve its IP.

    Returns:
        ``(is_valid, resolved_ip, reason)`` where:
        * ``is_valid`` is True if the endpoint passes all SSRF rules.
        * ``resolved_ip`` is the pinned IPv4/IPv6 string (or None on failure).
        * ``reason`` is a machine-readable failure reason (or None on success).
    """
    return resolve_and_validate_endpoint_url(url)

