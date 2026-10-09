"""Outbound network policy shared by every point of use (P-109).

One place for: the loopback host set, URL -> host, URL redaction for logs,
the link-local/metadata refusal, and DNS resolution of a host name before a
connection is made to it.

Users (all at the point of USE, because endpoints also arrive via env and
config import that bypass API validation):
* ``inference.urlcheck.validate_outbound_url`` (remote/AOAI decision and
  embedding calls, git skill sources);
* ``mcpclient.targets.validate_http_url`` (MCP server registration and every
  connector build);
* E3 adopts ``LOOPBACK_HOSTS`` for the decision edge.

Posture:
* Link-local and cloud-metadata addresses are ALWAYS refused: 169.254.0.0/16,
  fe80::/10, 100.100.100.200 (Alibaba-style metadata), fd00:ec2::254 (AWS IPv6
  IMDS), 64:ff9b:1::/48 (NAT64 local-use). IPv4 literals are normalised with
  ``inet_aton`` semantics (decimal/hex/octal/short-dotted spellings), and IPv6
  addresses embedding IPv4 (mapped, SIIT, compatible, NAT64) are unwrapped.
* RFC 1918 / ULA private ranges are ALLOWED (a LAN edge router is a normal
  remote).
* DNS names are resolved (``getaddrinfo``) and EVERY returned address is
  checked. A name that does not resolve passes (the connection cannot be made
  either); this check narrows, but cannot close, the DNS-rebinding window
  between the check and the connect.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

# Loopback host names (URL/Host forms; IPv6 with and without brackets).
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

FORBIDDEN_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "169.254.0.0/16",
        "fe80::/10",
        "100.100.100.200/32",
        "fd00:ec2::254/128",
        "64:ff9b:1::/48",  # NAT64 local-use (RFC 8215): translator-defined, refuse
    )
)
_EMBEDDED_V4_NETS = tuple(
    ipaddress.ip_network(n) for n in ("::ffff:0:0:0/96", "::/96", "64:ff9b::/96")
)

Resolver = Callable[..., list[tuple[Any, ...]]]
# Module seam: tests replace it (tests/support/settings.py) so the suite never
# does real DNS; production uses the system resolver.
_RESOLVER: Resolver = socket.getaddrinfo


class ForbiddenAddressError(ValueError):
    """The host is, or resolves to, a link-local/metadata address. Curated:
    the message never contains the host or URL."""


IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _unwrap(ip6: ipaddress.IPv6Address) -> IPAddress:
    if ip6.ipv4_mapped is not None:
        return ip6.ipv4_mapped
    if any(ip6 in net for net in _EMBEDDED_V4_NETS) and int(ip6) & 0xFFFFFFFF > 1:
        return ipaddress.IPv4Address(int(ip6) & 0xFFFFFFFF)
    return ip6


def literal_ip(host: str) -> IPAddress | None:
    """The address an IP-literal host will connect to, or None for a DNS name."""
    if host.startswith("[") or ":" in host:
        try:
            ip6 = ipaddress.IPv6Address(host.strip("[]").split("%", 1)[0])
        except ValueError:
            return None
        return _unwrap(ip6)
    try:
        # inet_aton accepts the decimal/hex/octal/short-dotted forms that
        # ipaddress rejects but getaddrinfo connects to.
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def is_forbidden(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        ip = _unwrap(ip)
    return any(ip in net for net in FORBIDDEN_NETS)


def host_of(url: str) -> str | None:
    """Lower-case host of `url` (IPv6 without brackets), or None if unparseable."""
    try:
        host = httpx.URL(url).host.lower()
    except (httpx.InvalidURL, ValueError, UnicodeError):
        return None
    return host or None


def redact_url(url: str) -> str:
    """`scheme://host[:port]` only: never userinfo, path, query or fragment."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "<invalid url>"
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


def resolve_and_check(host: str) -> None:
    """Refuse `host` if it is, or resolves to, a link-local/metadata address."""
    ip = literal_ip(host)
    if ip is not None:
        if is_forbidden(ip):
            raise ForbiddenAddressError("target must not be a link-local or metadata address")
        return
    try:
        infos = _RESOLVER(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return  # does not resolve: no connection can be made to it either
    for info in infos:
        try:
            resolved = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError:
            continue
        if is_forbidden(resolved):
            raise ForbiddenAddressError(
                "target must not resolve to a link-local or metadata address"
            )
