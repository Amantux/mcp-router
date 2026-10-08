"""Seeded catalog the `synthetic_v1` dataset is written against.

Direct ORM inserts (no discovery). Deliberately realistic and overlapping:
three `search_issues` variants (github / gitlab / jira) plus one on a
DISABLED server and one marked unavailable; two `list_tables`; an OFFLINE
server (gdrive); a tool with no embedding (jira/transition_ticket — reachable
by keyword only); an unclassified tool (web/fetch, domain=None).

`synthetic_scope_resolver` gives the dataset's agents their scopes:
  readonly-agent     read ceiling, all servers
  github-only-agent  github server only
  anyone else        unrestricted servers, execute ceiling (unknown-op denied)
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.interfaces import EmbeddingBackend, ScopeFilter
from mcprouter.models import MCPServerRecord, MCPToolRecord
from mcprouter.routing.scope import StaticScope

# server -> (domain, status, enabled, [(tool, description, operation, tags)])
_Tool = tuple[str, str, str, list[str]]
CATALOG: dict[str, tuple[str | None, str, bool, list[_Tool]]] = {
    "github": (
        "development",
        "healthy",
        True,
        [
            (
                "search_issues",
                "Search issues and pull requests across GitHub repositories using a query string.",
                "read",
                ["issues", "search"],
            ),
            (
                "get_issue",
                "Get the details of a specific GitHub issue by number.",
                "read",
                ["issues"],
            ),
            ("create_issue", "Create a new issue in a GitHub repository.", "write", ["issues"]),
            (
                "update_issue",
                "Update the title, body, labels or state of an existing GitHub issue.",
                "write",
                ["issues"],
            ),
            (
                "add_issue_comment",
                "Add a comment to a GitHub issue or pull request.",
                "write",
                ["issues", "comments"],
            ),
            (
                "list_pull_requests",
                "List pull requests in a GitHub repository, filtered by state.",
                "read",
                ["pull-requests"],
            ),
            (
                "get_pull_request",
                "Get the details and diff of a GitHub pull request.",
                "read",
                ["pull-requests"],
            ),
            (
                "merge_pull_request",
                "Merge an open GitHub pull request.",
                "write",
                ["pull-requests"],
            ),
            ("create_branch", "Create a new branch in a GitHub repository.", "write", ["git"]),
            (
                "search_code",
                "Search source code across GitHub repositories.",
                "read",
                ["code", "search"],
            ),
            (
                "get_file_contents",
                "Get the contents of a file or directory from a GitHub repository.",
                "read",
                ["files"],
            ),
        ],
    ),
    "gitlab": (
        "development",
        "healthy",
        True,
        [
            ("search_issues", "Search issues in GitLab projects.", "read", ["issues", "search"]),
            ("create_issue", "Create an issue in a GitLab project.", "write", ["issues"]),
            (
                "list_merge_requests",
                "List merge requests for a GitLab project.",
                "read",
                ["merge-requests"],
            ),
            (
                "get_pipeline_status",
                "Get the status of the latest CI pipeline for a GitLab project.",
                "read",
                ["ci"],
            ),
        ],
    ),
    "jira": (
        "productivity",
        "healthy",
        True,
        [
            (
                "search_issues",
                "Search Jira issues and tickets with JQL.",
                "read",
                ["tickets", "search"],
            ),
            ("create_ticket", "Create a Jira ticket in a project.", "write", ["tickets"]),
            (
                "transition_ticket",
                "Move a Jira ticket to another workflow status such as In Progress or Done.",
                "write",
                ["tickets", "workflow"],
            ),
            (
                "add_worklog",
                "Log time spent working on a Jira ticket.",
                "write",
                ["tickets", "time"],
            ),
        ],
    ),
    "slack": (
        "communication",
        "healthy",
        True,
        [
            ("send_message", "Send a message to a Slack channel or user.", "write", ["chat"]),
            ("search_messages", "Search Slack message history.", "read", ["chat", "search"]),
            ("list_channels", "List Slack channels in the workspace.", "read", ["chat"]),
            (
                "get_channel_history",
                "Fetch recent messages from a Slack channel.",
                "read",
                ["chat"],
            ),
            ("add_reaction", "Add an emoji reaction to a Slack message.", "write", ["chat"]),
        ],
    ),
    "email": (
        "communication",
        "healthy",
        True,
        [
            ("send_email", "Send an email to one or more recipients.", "write", ["mail"]),
            (
                "search_inbox",
                "Search the email inbox for messages matching a query.",
                "read",
                ["mail", "search"],
            ),
            ("read_email", "Read the full content of an email message.", "read", ["mail"]),
            ("create_draft", "Create an email draft without sending it.", "write", ["mail"]),
        ],
    ),
    "filesystem": (
        "files",
        "healthy",
        True,
        [
            ("read_file", "Read the contents of a file from the local filesystem.", "read", ["fs"]),
            ("write_file", "Write content to a file, creating or overwriting it.", "write", ["fs"]),
            ("list_directory", "List files and folders in a directory.", "read", ["fs"]),
            (
                "search_files",
                "Search for files by name pattern under a directory.",
                "read",
                ["fs", "search"],
            ),
            ("move_file", "Move or rename a file.", "write", ["fs"]),
            ("delete_file", "Delete a file from the local filesystem.", "write", ["fs"]),
        ],
    ),
    "gdrive": (
        "files",
        "offline",
        True,
        [
            (
                "search_drive",
                "Search Google Drive for documents and files.",
                "read",
                ["docs", "search"],
            ),
            ("download_file", "Download a file from Google Drive.", "read", ["docs"]),
        ],
    ),
    "s3": (
        "files",
        "healthy",
        True,
        [
            ("list_objects", "List objects in an S3 bucket.", "read", ["storage"]),
            ("upload_object", "Upload a file to an S3 bucket.", "write", ["storage"]),
        ],
    ),
    "postgres": (
        "databases",
        "healthy",
        True,
        [
            ("run_query", "Run a SQL query against the PostgreSQL database.", "execute", ["sql"]),
            ("list_tables", "List tables in the PostgreSQL database.", "read", ["sql", "schema"]),
            (
                "describe_table",
                "Describe the columns and types of a PostgreSQL table.",
                "read",
                ["sql", "schema"],
            ),
        ],
    ),
    "sqlite": (
        "databases",
        "healthy",
        True,
        [
            (
                "read_query",
                "Execute a read-only SELECT query on the SQLite database.",
                "read",
                ["sql"],
            ),
            (
                "write_query",
                "Execute an INSERT, UPDATE or DELETE statement on the SQLite database.",
                "write",
                ["sql"],
            ),
            ("list_tables", "List tables in the SQLite database.", "read", ["sql", "schema"]),
        ],
    ),
    "calendar": (
        "productivity",
        "healthy",
        True,
        [
            ("list_events", "List upcoming calendar events in a date range.", "read", ["calendar"]),
            ("create_event", "Create a calendar event with attendees.", "write", ["calendar"]),
            (
                "find_free_time",
                "Find free time slots across attendees' calendars.",
                "read",
                ["calendar", "scheduling"],
            ),
        ],
    ),
    "notion": (
        "productivity",
        "healthy",
        True,
        [
            ("search_pages", "Search Notion pages and databases.", "read", ["docs", "search"]),
            ("create_page", "Create a new Notion page.", "write", ["docs"]),
            ("append_block", "Append content to an existing Notion page.", "write", ["docs"]),
        ],
    ),
    "shell": (
        "development",
        "healthy",
        True,
        [
            ("run_command", "Run a shell command on the host machine.", "execute", ["exec"]),
        ],
    ),
    "web": (
        None,
        "healthy",
        True,
        [
            ("fetch", "Fetch a URL and return its content as markdown.", "read", ["http"]),
        ],
    ),
    "legacy_tracker": (
        "development",
        "healthy",
        False,
        [
            ("search_issues", "Search issues in the legacy bug tracker.", "read", ["issues"]),
        ],
    ),
    "linear": (
        "development",
        "healthy",
        True,
        [
            ("search_issues", "Search Linear issues.", "read", ["issues"]),
        ],
    ),
}
UNEMBEDDED = {("jira", "transition_ticket")}
UNAVAILABLE = {("linear", "search_issues")}


def embedding_text(name: str, description: str, tags: list[str]) -> str:
    return f"{name.replace('_', ' ')}: {description} {' '.join(tags)}"


def seed_synthetic_catalog(s: Session, embedder: EmbeddingBackend) -> dict[str, str]:
    """Insert the catalog; returns server name -> id. Caller commits."""
    server_ids: dict[str, str] = {}
    for server, (domain, status, enabled, tools) in CATALOG.items():
        srv = MCPServerRecord(name=server, transport="stdio", status=status, enabled=enabled)
        s.add(srv)
        s.flush()
        server_ids[server] = srv.id
        texts = [embedding_text(n, d, tg) for n, d, _, tg in tools]
        vectors = embedder.embed(texts)
        for (name, desc, op, tags), vec in zip(tools, vectors, strict=True):
            schema: dict[str, object] = {"type": "object", "properties": {}}
            t = MCPToolRecord(
                server_id=srv.id,
                name=name,
                description=desc,
                input_schema=schema,
                schema_hash=hashlib.sha256(json.dumps(schema).encode()).hexdigest(),
                domain=domain,
                operation=op,
                tags=tags,
                available=(server, name) not in UNAVAILABLE,
            )
            if (server, name) not in UNEMBEDDED:
                t.embedding = vec
                t.embedding_backend = embedder.name
            s.add(t)
    s.flush()
    return server_ids


def synthetic_scope_resolver(
    session_factory: sessionmaker[Session],
) -> Callable[[str], ScopeFilter]:
    with session_factory() as s:
        gh = s.scalar(select(MCPServerRecord.id).where(MCPServerRecord.name == "github"))
    scopes: dict[str, ScopeFilter] = {
        "readonly-agent": StaticScope(max_operation="read"),
        "github-only-agent": StaticScope(servers=(gh,) if gh else ()),
    }
    default = StaticScope()

    def resolve(agent_id: str) -> ScopeFilter:
        return scopes.get(agent_id, default)

    return resolve
