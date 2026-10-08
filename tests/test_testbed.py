"""The synthetic fleet: determinism, overlap ground truth, real SDK servers."""

from __future__ import annotations

from collections import Counter

import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from testbed.fleet import (
    DOMAINS,
    generate_fleet,
    manifest,
    spec_from_dict,
    with_description,
    with_extra_param,
    without_tool,
)
from testbed.harness import http_fleet
from testbed.serve import endpoint_urls
from testbed.servers import build_server

SERVE_PORT = 8603


def test_fleet_is_deterministic_and_unique() -> None:
    a, b = generate_fleet(120, tools=1300), generate_fleet(120, tools=1300)
    assert manifest(a) == manifest(b)
    assert len({s.name for s in a}) == 120
    for s in a:
        assert len({t.name for t in s.tools}) == len(s.tools), s.name


def test_exact_tool_total_and_all_domains() -> None:
    fleet = generate_fleet(100, tools=1000)
    assert sum(len(s.tools) for s in fleet) == 1000
    assert {s.domain for s in fleet} == set(DOMAINS)
    # Any short prefix already spans every domain (useful for small tests).
    assert {s.domain for s in generate_fleet(17)} == set(DOMAINS)


def test_padding_beyond_family_size() -> None:
    fleet = generate_fleet(2, tools=60)
    assert [len(s.tools) for s in fleet] == [30, 30]
    assert all(len({t.name for t in s.tools}) == 30 for s in fleet)


def test_near_duplicates_present_with_ground_truth() -> None:
    by_name = {s.name: s for s in generate_fleet(60)}
    gh, gl = by_name["github"].tool("search_issues"), by_name["gitlab"].tool("search_issues")
    assert gh.canonical == gl.canonical and gh.description != gl.description
    files_read, fs_get = by_name["files"].tool("read_file"), by_name["fs"].tool("get_file")
    assert files_read.canonical == fs_get.canonical == "filesystem.read_file"
    assert by_name["sqlite"].tool("execute_sql").canonical == "sql.run_query"
    # Many canonical capabilities are offered by >1 server.
    counts = Counter(t.canonical for s in by_name.values() for t in s.tools)
    assert sum(1 for c in counts.values() if c > 1) > 50


def test_operations_and_destructive_flags() -> None:
    ops = Counter(t.operation for s in generate_fleet(55) for t in s.tools)
    assert set(ops) == {"read", "write", "execute"}
    files = generate_fleet(10)[8]
    assert files.tool("delete_file").destructive and not files.tool("read_file").destructive


def test_mutations_and_roundtrip() -> None:
    spec = generate_fleet(1)[0]
    assert "get_issue" not in {t.name for t in without_tool(spec, "get_issue").tools}
    assert with_description(spec, "get_issue", "X").tool("get_issue").description == "X"
    changed = with_extra_param(spec, "get_issue", "verbose")
    assert "verbose" in changed.tool("get_issue").input_schema()["properties"]
    assert spec_from_dict(manifest([changed])[0]) == changed
    with pytest.raises(KeyError):
        without_tool(spec, "nope")


def test_endpoint_urls_spread_over_ports() -> None:
    urls = endpoint_urls(generate_fleet(5), port_base=8600, ports=2)
    assert urls["github"] == "http://127.0.0.1:8600/github/mcp"
    assert urls["jenkins"] == "http://127.0.0.1:8601/jenkins/mcp"


async def test_built_server_advertises_spec_schema() -> None:
    spec = generate_fleet(10)[8]
    async with Client(build_server(spec), cache=None) as c:
        listed = {t.name: t for t in (await c.list_tools()).tools}
    for t in spec.tools:
        assert listed[t.name].input_schema == t.input_schema()
        assert listed[t.name].description == t.description


async def test_serve_cli_hosts_multiple_servers_per_port() -> None:
    with http_fleet(4, port_base=SERVE_PORT, ports=1) as urls:
        assert len(urls) == 4 and len({u.split("/")[2] for u in urls.values()}) == 1
        for name, url in urls.items():
            async with Client(streamable_http_client(url), cache=None) as c:
                assert c.server_info is not None and c.server_info.name == name
