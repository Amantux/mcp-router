"""P-109: one outbound network policy; identical verdicts for MCP server
registration (targets.validate_http_url) and model config
(urlcheck.validate_outbound_url); DNS names checked after resolution."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest

from mcprouter import net_policy
from mcprouter.inference.urlcheck import InvalidEndpointError, validate_outbound_url
from mcprouter.mcpclient.errors import InvalidTargetError
from mcprouter.mcpclient.targets import ServerTarget, validate_http_url
from mcprouter.skills.gitsource import GitSourceError, fetch


def _fake_dns(table: dict[str, str]) -> Any:
    def resolver(host: str, port: object, **_: object) -> list[tuple[Any, ...]]:
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "no such host")
        ip = table[host]
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return resolver


DNS = {
    "metadata.evil.example": "169.254.169.254",
    "v6meta.evil.example": "fd00:ec2::254",
    "mapped.evil.example": "::ffff:169.254.169.254",
    "lan.example": "192.168.1.20",
    "public.example": "93.184.216.34",
}

# Every hostile URL is refused by BOTH validators (https so the model-config
# https rule is not what refuses it).
HOSTILE = [
    "https://169.254.169.254/latest",
    "https://[fe80::1]/x",
    "https://100.100.100.200/",
    "https://[fd00:ec2::254]/",
    "https://2852039166/",  # decimal 169.254.169.254
    "https://0xa9fea9fe/",
    "https://[::ffff:169.254.169.254]/",
    "https://metadata.evil.example/v1",
    "https://v6meta.evil.example/",
    "https://mapped.evil.example/",
    "https://user:pw@public.example/",
    "https://public.example/ x",
    "https://public.example:99999/",
]
ALLOWED = [
    "https://lan.example/mcp",
    "https://public.example/v1",
    "https://10.0.0.5:8443/",
    "https://unresolvable.example/",  # does not resolve: nothing to connect to
]


@pytest.fixture(autouse=True)
def _dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(net_policy, "_RESOLVER", _fake_dns(DNS))


@pytest.mark.parametrize("url", HOSTILE)
def test_hostile_refused_identically(url: str) -> None:
    with pytest.raises(InvalidTargetError) as server_exc:
        validate_http_url(url)
    with pytest.raises(InvalidEndpointError) as model_exc:
        validate_outbound_url(url)
    with pytest.raises(InvalidTargetError):
        ServerTarget(transport="streamable-http", endpoint=url).validated()
    for exc in (server_exc.value, model_exc.value):
        assert "evil" not in str(exc) and "169.254" not in str(exc) and "pw" not in str(exc)


@pytest.mark.parametrize("url", ALLOWED)
def test_allowed_identically(url: str) -> None:
    assert validate_http_url(url) == url
    assert validate_outbound_url(url) == url


def test_name_resolving_to_metadata_refused_via_getaddrinfo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    base = _fake_dns({"innocent.example": "169.254.169.254"})

    def spy(host: str, port: object, **kw: object) -> list[tuple[Any, ...]]:
        calls.append(host)
        return base(host, port, **kw)  # type: ignore[no-any-return]

    monkeypatch.setattr(net_policy, "_RESOLVER", spy)
    with pytest.raises(net_policy.ForbiddenAddressError):
        net_policy.resolve_and_check("innocent.example")
    assert calls == ["innocent.example"]


def test_any_forbidden_address_among_several_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    def multi(host: str, port: object, **_: object) -> list[tuple[Any, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0)),
        ]

    monkeypatch.setattr(net_policy, "_RESOLVER", multi)
    with pytest.raises(net_policy.ForbiddenAddressError):
        net_policy.resolve_and_check("rr.example")


def test_git_source_resolved_before_clone(tmp_path: Path) -> None:
    ran: list[object] = []
    with pytest.raises(GitSourceError):
        fetch(
            "0" * 8,
            "https://metadata.evil.example/repo.git",
            "main",
            tmp_path,
            runner=lambda argv: ran.append(argv),
        )
    assert ran == []  # refused before git ever ran


def test_loopback_hosts_and_helpers() -> None:
    assert {"localhost", "127.0.0.1", "::1", "[::1]"} == net_policy.LOOPBACK_HOSTS
    assert net_policy.host_of("https://User@Example.COM:8443/p?q=1") == "example.com"
    assert net_policy.host_of("https://[::1]:9/") == "::1"
    assert net_policy.host_of("http://[::1/") is None
    assert (
        net_policy.redact_url("https://u:pw@h.example:8443/p?token=x#f") == "https://h.example:8443"
    )
    assert net_policy.redact_url("http://[::1]/x") == "http://[::1]"
    assert net_policy.redact_url("https://h:bad/") == "<invalid url>"
