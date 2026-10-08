"""Outbound URL validation for remote inference backends (shared).

A thin wrapper over ``mcpclient.targets.validate_http_url`` (host required, no
userinfo, no whitespace, well-formed port) that adds the stricter rules an
outbound call carrying an API key needs: https only (plain http only to a
loopback host, for edge/dev), and no fragment.

Call it at the point of USE (immediately before each request), not only when
settings load: endpoints also arrive via env/add-on options that bypass any
API validation.

Network posture (owner decision, wave 3):
- Link-local and cloud-metadata addresses are ALWAYS refused when the host is
  an IP literal, for any scheme: 169.254.0.0/16, fe80::/10, 100.100.100.200
  (Alibaba-style metadata) and fd00:ec2::254 (AWS IPv6 IMDS).
  IPv4 hosts are normalised with ``inet_aton`` semantics first (what the
  resolver will actually connect to), so decimal (``2852039166``), hex
  (``0xa9fea9fe``), octal and short-dotted (``169.254.43518``) spellings are
  refused too. IPv6 hosts embedding an IPv4 address are unwrapped before the
  check: IPv4-mapped ``::ffff:0:0/96``, SIIT ``::ffff:0:0:0/96``,
  IPv4-compatible ``::/96`` (except ``::`` and ``::1``) and NAT64 ``64:ff9b::/96``.
- RFC 1918 / ULA private ranges are deliberately ALLOWED: a LAN edge router is
  the intended remote.
- DNS names are NOT resolved here. Residual: a DNS name that resolves to a
  link-local/metadata address is not caught by this validator.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

from mcprouter.inference.errors import InferenceError
from mcprouter.mcpclient.errors import InvalidTargetError
from mcprouter.mcpclient.targets import validate_http_url

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_FORBIDDEN_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "169.254.0.0/16",
        "fe80::/10",
        "100.100.100.200/32",
        "fd00:ec2::254/128",
        "64:ff9b:1::/48",  # NAT64 local-use (RFC 8215): translator-defined, refuse
    )
)
_MALFORMED = "endpoint URL is malformed"


_EMBEDDED_V4_NETS = tuple(
    ipaddress.ip_network(n) for n in ("::ffff:0:0:0/96", "::/96", "64:ff9b::/96")
)


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address an IP-literal host will connect to, or None for a DNS name."""
    if host.startswith("[") or ":" in host:
        try:
            ip6 = ipaddress.IPv6Address(host.strip("[]").split("%", 1)[0])
        except ValueError:
            return None
        if ip6.ipv4_mapped is not None:
            return ip6.ipv4_mapped
        if any(ip6 in net for net in _EMBEDDED_V4_NETS) and int(ip6) & 0xFFFFFFFF > 1:
            return ipaddress.IPv4Address(int(ip6) & 0xFFFFFFFF)
        return ip6
    try:
        # inet_aton accepts the decimal/hex/octal/short-dotted forms that
        # ipaddress rejects but getaddrinfo connects to.
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None  # a DNS name: not resolved here (documented residual)


def _forbidden_literal(host: str) -> bool:
    ip = _literal_ip(host)
    return ip is not None and any(ip in net for net in _FORBIDDEN_NETS)


class InvalidEndpointError(InferenceError):
    """The configured outbound endpoint is not acceptable. Curated message;
    never contains the URL itself (it may embed a token in its query)."""


def validate_outbound_url(url: str, *, allow_http_localhost: bool = True) -> str:
    """Return ``url`` unchanged if it is safe to send credentials to.

    Raises InvalidEndpointError otherwise (curated message; never echoes the
    host or URL). Link-local/metadata IP literals are always refused; private
    LAN ranges are allowed; DNS names are not resolved (see module docstring).
    """
    malformed = False
    host = ""
    try:
        # httpx's parser refuses NUL/control bytes, IDNA-invalid hosts and
        # unbalanced IPv6 brackets (InvalidURL / idna errors are ValueErrors).
        host = httpx.URL(url).host.lower()
    except (httpx.InvalidURL, ValueError, UnicodeError):
        malformed = True
    if malformed:  # raised outside the except: no chained parser error
        raise InvalidEndpointError(_MALFORMED)
    if _forbidden_literal(host):
        raise InvalidEndpointError("endpoint URL must not target a link-local or metadata address")
    try:
        validate_http_url(url)
    except InvalidTargetError as exc:
        # The targets messages are curated constants; safe to carry over.
        raise InvalidEndpointError(str(exc)) from None
    parts = urlsplit(url)
    if parts.fragment or "#" in url:
        raise InvalidEndpointError("endpoint URL must not contain a fragment")
    host = (parts.hostname or "").lower()
    if parts.scheme == "https":
        return url
    if allow_http_localhost and host in _LOOPBACK_HOSTS:
        return url
    raise InvalidEndpointError("endpoint URL must use https (http is allowed only for localhost)")
