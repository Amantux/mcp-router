"""Config import (Claude-Desktop mcpServers), credential storage, delete guard."""

from __future__ import annotations

import json
import logging

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from testbed.harness import stdio_command_for_index, stdio_env

from mcprouter.discovery import (
    DiscoveryService,
    ServerInUseError,
    ServerRegistration,
    delete_server,
    import_config,
    parse_mcp_servers_config,
)
from mcprouter.discovery.config_import import ConfigFormatError
from mcprouter.discovery.credentials import ServerCredentialRecord, get_env
from mcprouter.models import MCPServerRecord, MCPToolRecord, PolicyRule, ToolVersionRecord

from .conftest import requires_db

SF = sessionmaker[Session]
SECRET = "ghp_SUPERSECRETTOKEN123"


def _cfg(**servers: object) -> dict[str, object]:
    return {"mcpServers": servers}


# ------------------------------------------------------------------ parsing
def test_parse_valid_entries() -> None:
    items = parse_mcp_servers_config(
        _cfg(
            github={"command": "npx", "args": ["-y", "gh-mcp"], "env": {"GITHUB_TOKEN": SECRET}},
            remote={"url": "https://mcp.example.com/mcp"},
            legacy={"url": "https://mcp.example.com/sse", "type": "sse"},
            off={"url": "http://127.0.0.1:9/mcp", "disabled": True},
        )
    )
    by = {i.name: i.registration for i in items}
    assert all(i.error is None for i in items)
    gh = by["github"]
    assert gh is not None and gh.transport == "stdio" and gh.command == ("npx", "-y", "gh-mcp")
    assert gh.env == {"GITHUB_TOKEN": SECRET}
    assert by["remote"] is not None and by["remote"].transport == "streamable-http"
    assert by["legacy"] is not None and by["legacy"].transport == "sse"
    assert by["off"] is not None and by["off"].enabled is False


@pytest.mark.parametrize(
    ("entry", "fragment"),
    [
        ({"url": "http://user:pw@host/mcp"}, "credentials"),
        ({"url": "ftp://host/mcp"}, "http or https"),
        ({"url": "https://h/mcp", "headers": {"Authorization": "x"}}, "headers"),
        ({"url": "https://h/mcp", "type": "websocket"}, "unsupported"),
        ({"command": ""}, "command"),
        ({"command": "x", "args": [1]}, "args"),
        ({"command": "x", "env": {"K": 1}}, "env"),
        ({}, "command or a url"),
        ("nope", "object"),
    ],
)
def test_parse_rejects_bad_entries_individually(entry: object, fragment: str) -> None:
    items = parse_mcp_servers_config(_cfg(bad=entry, good={"url": "https://ok.example/mcp"}))
    errors = {i.name: i.error for i in items}
    assert errors["good"] is None
    assert errors["bad"] is not None and fragment in errors["bad"]


def test_errors_never_echo_env_values() -> None:
    items = parse_mcp_servers_config(_cfg(bad={"command": 7, "env": {"TOKEN": SECRET}}))
    assert items[0].error is not None and SECRET not in items[0].error


def test_registration_repr_hides_env() -> None:
    reg = ServerRegistration(name="x", transport="stdio", command=("x",), env={"T": SECRET})
    assert SECRET not in repr(reg)


@pytest.mark.parametrize("raw", ["{not json", json.dumps({"servers": {}}), json.dumps([1])])
def test_parse_rejects_bad_documents(raw: str) -> None:
    with pytest.raises(ConfigFormatError):
        parse_mcp_servers_config(raw)


# ------------------------------------------------------------ DB-backed import
@requires_db
def test_import_stores_env_as_credentials_and_skips_duplicates(
    db: SF, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    cfg = _cfg(
        gh={"command": "npx", "args": ["gh"], "env": {"GITHUB_TOKEN": SECRET}},
        web={"url": "https://mcp.example.com/mcp"},
        bad={"url": "http://u:p@h/mcp"},
    )
    with db() as s, s.begin():
        rep = import_config(s, json.dumps(cfg))
    assert sorted(r.name for r in rep.created) == ["gh", "web"]
    assert [n for n, _ in rep.skipped] == ["bad"]
    with db() as s:
        gh = s.scalar(select(MCPServerRecord).where(MCPServerRecord.name == "gh"))
        assert gh is not None and gh.stdio_command == ["npx", "gh"]
        assert get_env(s, gh.id) == {"GITHUB_TOKEN": SECRET}

    with db() as s, s.begin():  # re-import: existing names skipped, never overwritten
        rep2 = import_config(s, cfg)
    assert rep2.created == [] and {n for n, _ in rep2.skipped} == {"gh", "web", "bad"}
    assert SECRET not in caplog.text


@requires_db
async def test_imported_stdio_server_discovers_with_stored_env(
    db: SF, caplog: pytest.LogCaptureFixture
) -> None:
    """End to end: the env from the config is what lets the real stdio
    subprocess start (it needs PYTHONPATH) — proves credentials are used."""
    caplog.set_level(logging.DEBUG)
    cmd = stdio_command_for_index(20, 5)  # "slack"
    env = {**stdio_env(), "SLACK_TOKEN": SECRET}
    with db() as s, s.begin():
        rep = import_config(s, _cfg(slack={"command": cmd[0], "args": cmd[1:], "env": env}))
    sid = rep.created[0].id
    report = await DiscoveryService(db).sync_server(sid)
    assert "send_message" in report.added
    assert SECRET not in caplog.text


# --------------------------------------------------------------- delete guard
@requires_db
async def test_delete_refused_while_policy_rule_references_server(db: SF) -> None:
    cmd = stdio_command_for_index(20, 0)
    with db() as s, s.begin():
        rep = import_config(s, _cfg(gh={"command": cmd[0], "args": cmd[1:], "env": stdio_env()}))
    sid = rep.created[0].id
    await DiscoveryService(db).sync_server(sid)
    with db() as s, s.begin():
        s.add(PolicyRule(agent_id="a1", server_id=sid, tool_name="search_*"))
        s.add(PolicyRule(agent_id="a2", server_id=None))  # any-server rule: not a pin

    with db() as s, s.begin(), pytest.raises(ServerInUseError):
        delete_server(s, sid)

    with db() as s, s.begin():
        s.execute(PolicyRule.__table__.delete().where(PolicyRule.server_id == sid))
    with db() as s, s.begin():
        delete_server(s, sid)
    with db() as s:
        assert s.get(MCPServerRecord, sid) is None
        assert s.get(ServerCredentialRecord, sid) is None
        n_tools = s.scalar(
            select(func.count()).select_from(MCPToolRecord).where(MCPToolRecord.server_id == sid)
        )
        n_versions = s.scalar(select(func.count()).select_from(ToolVersionRecord))
    assert n_tools == 0 and n_versions == 0


def test_disabled_must_be_a_real_boolean() -> None:
    items = parse_mcp_servers_config(_cfg(x={"url": "https://h/mcp", "disabled": "false"}))
    assert items[0].error == "disabled must be true or false"


@requires_db
def test_seed_refuses_to_repoint_a_real_server(db: SF) -> None:
    from testbed.fleet import generate_fleet
    from testbed.seed import register_fleet

    with db() as s, s.begin():
        s.add(
            MCPServerRecord(
                id="11111111-1111-1111-1111-111111111111",
                name="github",
                transport="streamable-http",
                endpoint="https://api.githubcopilot.example/mcp",
            )
        )
    with pytest.raises(SystemExit):
        register_fleet(db, generate_fleet(1), lambda n: f"http://127.0.0.1:8600/{n}/mcp")
    with db() as s:
        srv = s.get(MCPServerRecord, "11111111-1111-1111-1111-111111111111")
        assert srv is not None and srv.endpoint == "https://api.githubcopilot.example/mcp"
