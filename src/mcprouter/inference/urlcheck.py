"""Outbound URL validation for remote inference backends (shared).

A thin wrapper over ``mcpclient.targets.validate_http_url`` (host required, no
userinfo, no whitespace, well-formed port) that adds the stricter rules an
outbound call carrying an API key needs: https only (plain http only to a
loopback host, for edge/dev), and no fragment.

Call it at the point of USE (immediately before each request), not only when
settings load: endpoints also arrive via env/add-on options that bypass any
API validation.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from mcprouter.inference.errors import InferenceError
from mcprouter.mcpclient.errors import InvalidTargetError
from mcprouter.mcpclient.targets import validate_http_url

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class InvalidEndpointError(InferenceError):
    """The configured outbound endpoint is not acceptable. Curated message;
    never contains the URL itself (it may embed a token in its query)."""


def validate_outbound_url(url: str, *, allow_http_localhost: bool = True) -> str:
    """Return ``url`` unchanged if it is safe to send credentials to.

    Raises InvalidEndpointError otherwise.
    """
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
