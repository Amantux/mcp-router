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
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.interfaces import EmbeddingBackend, ScopeFilter
from mcprouter.models import MCPServerRecord, MCPToolRecord, SkillRecord, SkillSourceRecord
from mcprouter.registry.classify import classify_skill
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


# --------------------------------------------- appended: Wave-4 skills fixture
# Two sources, 15 spec-valid skills over the five tool domains. Deliberate
# hazards: near-duplicate pairs (team-skills/pdf-fill ~ community-skills/
# pdf-form-filler, team-skills/pr-review ~ community-skills/pr-reviewer), two
# execute-class skills (has_scripts + Bash), and skills whose descriptions
# overlap a seeded TOOL (pdf-fill vs filesystem/write_file, sql-report vs
# postgres/run_query, email-triage vs email/read_email, ...) for mixed cases.
# Classification is GROUND TRUTH set directly and marked reviewed, so an
# auto-classifier pass can never move it.

SKILL_SOURCES: tuple[str, ...] = ("team-skills", "community-skills")
SKILL_CLASSIFICATION_SOURCE = "synthetic-ground-truth"


@dataclass(frozen=True)
class SyntheticSkill:
    source: str
    name: str
    description: str
    body: str
    domain: str
    operation: str  # read | write | execute
    has_scripts: bool = False
    allowed_tools: tuple[str, ...] = ()

    @property
    def ref(self) -> str:
        return f"{self.source}/{self.name}"

    @property
    def body_tokens_est(self) -> int:
        return len(self.body) // 4


def _sk(
    source: str, name: str, desc: str, body: str, domain: str, op: str, **kw: Any
) -> SyntheticSkill:
    return SyntheticSkill(source, name, desc, body, domain, op, **kw)


_T, _C = SKILL_SOURCES
SKILLS: tuple[SyntheticSkill, ...] = (
    _sk(_T, "pdf-fill", "Fill in PDF form fields from supplied values and write the completed PDF.",
        "Map each supplied value onto the matching AcroForm field, flatten, save a copy.",
        "files", "write"),
    _sk(_T, "pdf-extract", "Extract text and tables from PDF documents for analysis.",
        "Read each page, keep reading order, return tables as markdown.", "files", "read"),
    _sk(_T, "release-notes", "Write release notes from the pull requests merged since the last tag.",
        "Group merged pull requests by label, one bullet each, breaking changes first.",
        "development", "write"),
    _sk(_T, "pr-review", "Review a pull request diff for bugs, missing tests and risky changes.",
        "Read the diff hunk by hunk; report findings with file and line.", "development", "read"),
    _sk(_T, "deploy-service", "Deploy a service to staging or production with the bundled scripts.",
        "Use scripts/deploy.sh with the target environment; check health afterwards.",
        "development", "execute", has_scripts=True, allowed_tools=("Bash",)),
    _sk(_T, "sql-report", "Compose a SQL query for a business question and summarize the rows as a report.",
        "Inspect the schema first, prefer aggregates, cite the query used.", "databases", "read"),
    _sk(_T, "schema-migration", "Apply a database schema migration with the bundled migration scripts.",
        "Use scripts/migrate.sh against the target database after a backup.",
        "databases", "execute", has_scripts=True, allowed_tools=("Bash",)),
    _sk(_T, "standup-summary", "Summarize recent Slack channel activity into a daily standup digest.",
        "Bucket messages into done, doing and blocked; one line per person.",
        "communication", "read"),
    _sk(_T, "email-triage", "Triage an inbox: categorize and prioritize unread email messages.",
        "Label each message urgent, normal or ignore; list the urgent ones first.",
        "communication", "read"),
    _sk(_T, "meeting-scheduler", "Plan a meeting: find a slot that suits all attendees and create the calendar invite.",
        "Check every attendee's availability, propose two options, then book one.",
        "productivity", "write"),
    _sk(_T, "meeting-notes", "Write structured meeting notes with decisions and action items.",
        "Sections: attendees, decisions, action items with owners and due dates.",
        "productivity", "write"),
    _sk(_C, "pdf-form-filler", "Fill PDF forms automatically from structured data and write out the filled form.",
        "Match keys to form field names, fill, save next to the original.", "files", "write"),
    _sk(_C, "pr-reviewer", "Review pull requests and comment on bugs and code quality problems.",
        "Look at the changed files and summarise the problems found.", "development", "read"),
    _sk(_C, "csv-cleanup", "Clean and normalize a CSV file and write the cleaned copy.",
        "Trim whitespace, unify date formats, drop duplicate rows.", "files", "write"),
    _sk(_C, "incident-postmortem", "Create a blameless incident postmortem document from a timeline.",
        "Sections: summary, impact, timeline, root cause, follow-ups.", "development", "write"),
)  # fmt: skip
NEAR_DUPLICATE_SKILLS = (
    ("team-skills/pdf-fill", "community-skills/pdf-form-filler"),
    ("team-skills/pr-review", "community-skills/pr-reviewer"),
)


def _stable_id(kind: str, key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mcprouter-synthetic:{kind}:{key}"))


def skill_classification_mismatches() -> list[tuple[str, str, str]]:
    """(ref, ground_truth_op, classifier_op) wherever the rule-based skill
    classifier disagrees with the fixture's ground-truth operation."""
    out: list[tuple[str, str, str]] = []
    for sk in SKILLS:
        c = classify_skill(
            sk.name,
            sk.description,
            sk.body,
            has_scripts=sk.has_scripts,
            allowed_tools=list(sk.allowed_tools),
        )
        if c.operation != sk.operation:
            out.append((sk.ref, sk.operation, c.operation))
    return out


def seed_synthetic_skills(s: Session, embedder: EmbeddingBackend) -> dict[str, str]:
    """Insert the skill sources + skills with deterministic ids and
    ground-truth (reviewed) classification; returns source name -> id.
    Caller commits."""
    source_ids: dict[str, str] = {}
    for src in SKILL_SOURCES:
        rec = SkillSourceRecord(
            id=_stable_id("source", src),
            name=src,
            kind="directory",
            location=f"/synthetic/{src}",
            status="healthy",
        )
        s.add(rec)
        source_ids[src] = rec.id
    s.flush()
    vectors = embedder.embed([f"{sk.name.replace('-', ' ')}: {sk.description}" for sk in SKILLS])
    for sk, vec in zip(SKILLS, vectors, strict=True):
        digest = hashlib.sha256(f"{sk.description}\n{sk.body}".encode()).hexdigest()
        s.add(
            SkillRecord(
                id=_stable_id("skill", sk.ref),
                source_id=source_ids[sk.source],
                name=sk.name,
                description=sk.description,
                body=sk.body,
                relative_path=sk.name,
                allowed_tools=list(sk.allowed_tools),
                has_scripts=sk.has_scripts,
                resource_manifest=(
                    [{"path": "scripts/run.sh", "sha256": digest}] if sk.has_scripts else []
                ),
                content_hash=digest,
                manifest_hash=hashlib.sha256(sk.ref.encode()).hexdigest(),
                body_tokens_est=sk.body_tokens_est,
                domain=sk.domain,
                operation=sk.operation,
                classification_reviewed=True,
                classification_source=SKILL_CLASSIFICATION_SOURCE,
                embedding=vec,
                embedding_backend=embedder.name,
            )
        )
    s.flush()
    return source_ids
