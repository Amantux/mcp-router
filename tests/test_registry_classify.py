"""Rule-based baseline classifier: deterministic, conservative (unknown > wrong)."""

from __future__ import annotations

import pytest

from mcprouter.registry.classify import RuleBasedClassifier, ToolClassifier, split_identifier

clf = RuleBasedClassifier()


def test_is_a_tool_classifier() -> None:
    assert isinstance(clf, ToolClassifier)
    assert clf.name == "rules-v1"


@pytest.mark.parametrize(
    ("raw", "tokens"),
    [
        ("list_issues", ["list", "issues"]),
        ("createIssue", ["create", "issue"]),
        ("github.create-pull-request", ["github", "create", "pull", "request"]),
        ("HTTPGet", ["http", "get"]),
    ],
)
def test_split_identifier(raw: str, tokens: list[str]) -> None:
    assert split_identifier(raw) == tokens


@pytest.mark.parametrize(
    ("name", "op"),
    [
        ("get_user", "read"),
        ("list_issues", "read"),
        ("search_code", "read"),
        ("read_file", "read"),
        ("createIssue", "write"),
        ("update_page", "write"),
        ("send_message", "write"),
        ("write_file", "write"),
        ("run_query", "execute"),  # lead verb wins over a less severe later verb
        ("exec_command", "execute"),
        ("delete_branch", "execute"),
        ("deploy_service", "execute"),
        ("github_create_issue", "write"),  # namespace prefix before the verb
    ],
)
def test_operation_from_name(name: str, op: str) -> None:
    assert clf.classify(name, "", {}).operation == op


@pytest.mark.parametrize(
    "name",
    [
        "get_or_delete_user",  # later verb more severe than the lead -> refuse to guess
        "get_or_create_user",
        "frobnicate",  # no known verb anywhere
        "weather",
    ],
)
def test_operation_conservative_unknown(name: str) -> None:
    assert clf.classify(name, "", {}).operation == "unknown"


def test_description_first_word_fallback_when_name_has_no_verb() -> None:
    assert clf.classify("issues", "Lists open issues in a repo.", {}).operation == "read"
    assert clf.classify("issues", "Creates an issue.", {}).operation == "write"
    assert clf.classify("branch", "Deletes a branch.", {}).operation == "execute"
    # Verb later in the description is NOT trusted.
    assert clf.classify("issues", "This tool can delete things.", {}).operation == "unknown"


def test_name_verb_wins_over_description() -> None:
    assert clf.classify("get_issue", "Deletes nothing; returns an issue.", {}).operation == "read"


def test_execute_schema_hint_downgrades_read_or_write_to_unknown() -> None:
    schema = {"type": "object", "properties": {"command": {"type": "string"}}}
    assert clf.classify("get_status", "", schema).operation == "unknown"
    assert clf.classify("run_task", "", schema).operation == "execute"


@pytest.mark.parametrize(
    ("name", "desc", "domain"),
    [
        ("list_pull_requests", "List pull requests in a GitHub repository", "development"),
        ("send_message", "Send a Slack message to a channel", "communication"),
        ("read_file", "Read a file from the filesystem", "files"),
        ("run_query", "Run a SQL query against the Postgres database", "databases"),
        ("create_event", "Create a calendar event", "productivity"),
    ],
)
def test_domain_from_keywords(name: str, desc: str, domain: str) -> None:
    assert clf.classify(name, desc, {}).domain == domain


def test_domain_none_when_no_signal_or_tie() -> None:
    assert clf.classify("frobnicate", "Does a thing.", {}).domain is None
    # One development keyword (repo) vs one communication keyword (email) -> tie -> None.
    assert clf.classify("notify", "Email about a repo", {}).domain is None


def test_deterministic_and_explained() -> None:
    a = clf.classify("send_message", "Send a Slack message", {})
    b = clf.classify("send_message", "Send a Slack message", {})
    assert a == b
    assert a.evidence  # human-readable reasons
