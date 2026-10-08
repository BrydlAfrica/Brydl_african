"""SSRF defense for RedLab outbound model calls.

Before any client-private API key is sent to a ``Target.endpoint``, we
validate the URL and resolve its hostname, rejecting any address that
would route to an internal network, the cloud metadata service, or the
loopback interface (except in local-dev mode, where ``http://localhost``
is allowed so `mockmodel` / integration tests can run).

Design notes
------------
* **Hostname resolution happens inside the validator, not in httpx.**
  If we let httpx resolve and connect, the request could reach a
  blocked IP before our check runs (TOCTOU). We resolve with
  ``socket.getaddrinfo`` (respecting the system resolver, including
  Docker / VPN DNS) and reject if *any* returned address falls in a
  forbidden range. This doesn't perfectly defend against DNS-rebinding
  attacks where a hostname switches answers between our check and
  httpx's connect, but it defeats the 99% case and a tighter
  transport-layer guard would require a custom SOCKS/connect hook
  (deferred to Phase 9 hardening).
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
    # IPv6 loopback (::1/128)
    ipaddress.ip_network("::1/128"),
    # IPv6 unique-local (fc00::/7) — equivalent of RFC1918.
    ipaddress.ip_network("fc00::/7"),
    # IPv6 link-local (fe80::/10)
    ipaddress.ip_network("fe80::/10"),

]


def _http_local_allowed() -> bool:
    """Return True if local http loopback is permitted this call."""
    return os.environ.get(
        "REDLAB_ALLOW_HTTP_LOCAL", ""
    ).strip().lower() in ("1", "true", "yes")


def _is_blocked_ip(ip_str: str) -> bool:
    """Return True if the IP string falls in any blocked CIDR."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable address — treat as blocked
    for net in _BLOCKED_NETS:
        if ip.version == net.version and ip in net:
            return True
    return False


def validate_endpoint_url(url: str) -> tuple[bool, str | None]:
    """Validate an outbound LLM endpoint URL against SSRF rules.

    Args:
        url: The full endpoint URL (e.g.
            ``"https://api.openai.com/v1/chat/completions"``).

    Returns:
        ``(True, None)`` if the URL passes every check.
        ``(False, reason)`` with a short machine-readable reason if not:

        - ``"invalid_url"`` — malformed URL / missing scheme/host.
        - ``"scheme_not_https"`` — plain http outside the loopback
          dev exception.
        - ``"hostname_blocked"`` — hostname resolves to a forbidden
          IP range (loopback/RFC1918/link-local/metadata/ULA).
        - ``"dns_resolution_failed"`` — hostname didn't resolve.
    """
    if not url or not isinstance(url, str):
        return False, "invalid_url"

    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False, "invalid_url"

    if not parsed.scheme or not parsed.hostname:
        return False, "invalid_url"

    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()

    if scheme == "https":
        pass  # ok
    elif scheme == "http":
        if not _http_local_allowed():
            return False, "scheme_not_https"
        # http only allowed for explicit loopback hosts; for these
        # hosts we also SKIP the DNS/IP-block check below (loopback
        # is intentionally permitted in dev).
        if host in ("localhost", "127.0.0.1", "::1"):
            return True, None
        return False, "scheme_not_https"
    else:
        return False, "scheme_not_https"

    # Hostnames that are IP-literal strings — check directly, no DNS.
    try:
        ip = ipaddress.ip_address(host)
        if _is_blocked_ip(str(ip)):
            return False, "hostname_blocked"
        return True, None
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
    except socket.gaierror:
        logger.warning("RedLab SSRF: DNS resolution failed for %s", host)
        return False, "dns_resolution_failed"

    for family, _type, _proto, _canonname, sockaddr in infos:
        ip_str = sockaddr[0]
        if _is_blocked_ip(ip_str):
            logger.warning(
                "RedLab SSRF: host %s resolves to blocked IP %s",
                host, ip_str,
            )
            return False, "hostname_blocked"

    return True, None
