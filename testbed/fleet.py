"""Deterministic synthetic fleet generator.

The catalog is built from *families* (an issue tracker, a chat system, a SQL
database ...). Several services implement each family, so the generated fleet
has the overlap real deployments have:

* same tool name on different servers — ``github.search_issues`` vs
  ``gitlab.search_issues``;
* same capability under a different name — ``files.read_file`` vs
  ``fs.get_file``, ``postgres.run_query`` vs ``sqlite.execute_sql``.

Every tool carries ground truth for evaluation: ``domain`` (FR-04 domains),
``operation`` (read|write|execute) and ``canonical`` — the family-level
capability id shared by every near-duplicate (dedup ground truth).

Generation is a pure function of its arguments: same inputs, same fleet.

CLI: ``python -m testbed.fleet --servers 10 [--tools N]`` prints the manifest.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field, replace
from typing import Any

DOMAINS = ("development", "communication", "files", "databases", "productivity")

_PY_TYPES = {"str": "string", "int": "integer", "bool": "boolean", "float": "number"}

# Human descriptions for common parameters; anything else is humanized.
_PARAM_DOCS = {
    "query": "Free-text search query.",
    "limit": "Maximum number of results to return.",
    "path": "Absolute or workspace-relative path.",
    "repo": "Repository in owner/name form.",
    "issue_id": "Identifier of the issue.",
    "channel": "Channel name or id.",
    "text": "Message body.",
    "to": "Recipient address.",
    "subject": "Subject line.",
    "body": "Body content.",
    "sql": "SQL statement to run.",
    "table": "Table name.",
    "database": "Database name.",
    "collection": "Collection name.",
    "key": "Key to read or write.",
    "value": "Value to store.",
    "page_id": "Identifier of the page.",
    "title": "Title.",
    "content": "Content to write.",
    "start": "Start time (ISO 8601).",
    "end": "End time (ISO 8601).",
    "task_id": "Identifier of the task.",
    "project": "Project key or name.",
    "file_id": "Identifier of the file.",
    "destination": "Destination path.",
    "recursive": "Apply recursively.",
}


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: str  # JSON-schema primitive: string|integer|boolean|number
    required: bool
    description: str


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: tuple[ParamSpec, ...]
    domain: str
    operation: str  # read|write|execute
    destructive: bool
    canonical: str  # family.capability — shared by every near-duplicate

    def input_schema(self) -> dict[str, Any]:
        """The JSON schema an MCP client will see (mirrors the server side)."""
        return {
            "type": "object",
            "properties": {
                p.name: {"type": p.type, "description": p.description} for p in self.params
            },
            "required": [p.name for p in self.params if p.required],
        }


@dataclass(frozen=True)
class ServerSpec:
    name: str  # unique, used as server name and URL slug
    service: str  # e.g. "github"
    family: str  # e.g. "code_host"
    domain: str
    version: str
    tools: tuple[ToolSpec, ...] = field(default_factory=tuple)

    def tool(self, name: str) -> ToolSpec:
        for t in self.tools:
            if t.name == name:
                return t
        raise KeyError(name)


# --------------------------------------------------------------------- catalog
# Template mini-DSL: (name, description, op, params)
#   op: read | write | execute, with a trailing "!" for destructive
#   params: "name:type*" comma-separated, "*" marks required
_T = tuple[str, str, str, str]


@dataclass(frozen=True)
class _Family:
    domain: str
    services: tuple[tuple[str, str], ...]  # (service id, display name)
    templates: tuple[_T, ...]
    # Per-service renames: service -> {template name -> tool name}. These make
    # the "same capability, different name" near-duplicates.
    renames: dict[str, dict[str, str]] = field(default_factory=dict)


_FAMILIES: dict[str, _Family] = {
    "code_host": _Family(
        "development",
        (
            ("github", "GitHub"),
            ("gitlab", "GitLab"),
            ("bitbucket", "Bitbucket"),
            ("gitea", "Gitea"),
        ),
        (
            (
                "search_issues",
                "Search issues in {svc} repositories by text, label or state.",
                "read",
                "query:str*,repo:str,state:str,limit:int",
            ),
            (
                "get_issue",
                "Get a single {svc} issue with its comments.",
                "read",
                "repo:str*,issue_id:int*",
            ),
            (
                "create_issue",
                "Open a new issue in a {svc} repository.",
                "write",
                "repo:str*,title:str*,body:str,labels:str",
            ),
            (
                "comment_on_issue",
                "Add a comment to a {svc} issue.",
                "write",
                "repo:str*,issue_id:int*,body:str*",
            ),
            (
                "list_pull_requests",
                "List pull requests in a {svc} repository.",
                "read",
                "repo:str*,state:str,limit:int",
            ),
            (
                "get_pull_request",
                "Get a {svc} pull request including diff stats.",
                "read",
                "repo:str*,number:int*",
            ),
            (
                "create_pull_request",
                "Open a pull request on {svc} from a branch.",
                "write",
                "repo:str*,title:str*,head:str*,base:str*,body:str",
            ),
            (
                "merge_pull_request",
                "Merge an open {svc} pull request.",
                "write!",
                "repo:str*,number:int*,method:str",
            ),
            (
                "search_code",
                "Search source code across {svc} repositories.",
                "read",
                "query:str*,repo:str,limit:int",
            ),
            (
                "get_file_contents",
                "Read a file from a {svc} repository at a ref.",
                "read",
                "repo:str*,path:str*,ref:str",
            ),
            (
                "list_commits",
                "List recent commits on a {svc} branch.",
                "read",
                "repo:str*,branch:str,limit:int",
            ),
            (
                "create_branch",
                "Create a branch in a {svc} repository.",
                "write",
                "repo:str*,branch:str*,from_ref:str",
            ),
        ),
    ),
    "ci": _Family(
        "development",
        (("jenkins", "Jenkins"), ("circleci", "CircleCI"), ("buildkite", "Buildkite")),
        (
            ("list_pipelines", "List {svc} pipelines for a project.", "read", "project:str*"),
            (
                "get_pipeline_status",
                "Get the status of a {svc} pipeline run.",
                "read",
                "project:str*,run_id:str*",
            ),
            (
                "trigger_build",
                "Trigger a new {svc} build for a branch.",
                "execute",
                "project:str*,branch:str*",
            ),
            (
                "cancel_build",
                "Cancel a running {svc} build.",
                "execute!",
                "project:str*,run_id:str*",
            ),
            (
                "get_build_logs",
                "Fetch the logs of a {svc} build step.",
                "read",
                "project:str*,run_id:str*,step:str",
            ),
            (
                "list_artifacts",
                "List artifacts produced by a {svc} build.",
                "read",
                "project:str*,run_id:str*",
            ),
            ("retry_job", "Retry a failed {svc} job.", "execute", "project:str*,job_id:str*"),
            ("list_runners", "List {svc} build agents and their state.", "read", "limit:int"),
            (
                "get_test_report",
                "Get the test report of a {svc} build.",
                "read",
                "project:str*,run_id:str*",
            ),
            (
                "list_environments",
                "List deployment environments configured in {svc}.",
                "read",
                "project:str*",
            ),
        ),
    ),
    "tracker": _Family(
        "development",
        (("jira", "Jira"), ("linear", "Linear"), ("youtrack", "YouTrack")),
        (
            (
                "search_tickets",
                "Search {svc} tickets with a text query.",
                "read",
                "query:str*,project:str,limit:int",
            ),
            ("get_ticket", "Get a {svc} ticket by key.", "read", "ticket:str*"),
            (
                "create_ticket",
                "Create a {svc} ticket.",
                "write",
                "project:str*,title:str*,description:str,priority:str",
            ),
            (
                "update_ticket_status",
                "Move a {svc} ticket to another status.",
                "write",
                "ticket:str*,status:str*",
            ),
            ("assign_ticket", "Assign a {svc} ticket to a user.", "write", "ticket:str*,user:str*"),
            ("add_comment", "Comment on a {svc} ticket.", "write", "ticket:str*,body:str*"),
            ("list_sprints", "List active and upcoming {svc} sprints.", "read", "project:str*"),
            ("get_sprint_board", "Get the {svc} board for a sprint.", "read", "sprint_id:str*"),
            (
                "link_tickets",
                "Link two {svc} tickets.",
                "write",
                "ticket:str*,other:str*,relation:str",
            ),
            ("list_projects", "List {svc} projects.", "read", "limit:int"),
        ),
    ),
    "observability": _Family(
        "development",
        (("sentry", "Sentry"), ("datadog", "Datadog"), ("grafana", "Grafana")),
        (
            (
                "search_errors",
                "Search recent application errors in {svc}.",
                "read",
                "query:str*,environment:str,limit:int",
            ),
            (
                "get_error_event",
                "Get one {svc} error event with stack trace.",
                "read",
                "event_id:str*",
            ),
            ("resolve_issue", "Mark a {svc} issue as resolved.", "write", "issue_id:str*"),
            (
                "query_metrics",
                "Query a {svc} metric time series.",
                "read",
                "query:str*,start:str*,end:str",
            ),
            ("list_alerts", "List firing {svc} alerts.", "read", "state:str,limit:int"),
            (
                "mute_alert",
                "Silence a {svc} alert for a period.",
                "write",
                "alert_id:str*,minutes:int",
            ),
            ("get_dashboard", "Get a {svc} dashboard definition.", "read", "dashboard_id:str*"),
            ("list_services", "List services monitored by {svc}.", "read", "limit:int"),
            ("get_trace", "Get a distributed trace from {svc}.", "read", "trace_id:str*"),
            (
                "create_annotation",
                "Add a deploy annotation to {svc} graphs.",
                "write",
                "text:str*,tags:str",
            ),
        ),
    ),
    "containers": _Family(
        "development",
        (("docker", "Docker"), ("kubernetes", "Kubernetes"), ("nomad", "Nomad")),
        (
            ("list_containers", "List running {svc} workloads.", "read", "namespace:str"),
            ("get_container_logs", "Tail logs of a {svc} workload.", "read", "name:str*,lines:int"),
            ("restart_container", "Restart a {svc} workload.", "execute", "name:str*"),
            (
                "exec_command",
                "Run a shell command inside a {svc} workload.",
                "execute!",
                "name:str*,command:str*",
            ),
            ("list_images", "List {svc} images available locally.", "read", "limit:int"),
            ("pull_image", "Pull an image into {svc}.", "execute", "image:str*"),
            (
                "scale_deployment",
                "Scale a {svc} deployment to N replicas.",
                "execute",
                "name:str*,replicas:int*",
            ),
            ("get_pod_status", "Get health and status of a {svc} workload.", "read", "name:str*"),
            ("apply_manifest", "Apply a manifest to {svc}.", "execute!", "manifest:str*"),
            ("delete_pod", "Delete a {svc} workload instance.", "execute!", "name:str*"),
        ),
    ),
    "chat": _Family(
        "communication",
        (
            ("slack", "Slack"),
            ("discord", "Discord"),
            ("teams", "Microsoft Teams"),
            ("mattermost", "Mattermost"),
        ),
        (
            (
                "send_message",
                "Post a message to a {svc} channel.",
                "write",
                "channel:str*,text:str*,thread:str",
            ),
            (
                "search_messages",
                "Search {svc} message history.",
                "read",
                "query:str*,channel:str,limit:int",
            ),
            ("list_channels", "List {svc} channels you can see.", "read", "limit:int"),
            (
                "get_channel_history",
                "Read recent messages in a {svc} channel.",
                "read",
                "channel:str*,limit:int",
            ),
            ("create_channel", "Create a {svc} channel.", "write", "name:str*,private:bool"),
            ("invite_user", "Invite a user to a {svc} channel.", "write", "channel:str*,user:str*"),
            (
                "add_reaction",
                "React to a {svc} message with an emoji.",
                "write",
                "channel:str*,message_id:str*,emoji:str*",
            ),
            (
                "upload_attachment",
                "Upload a file to a {svc} channel.",
                "write",
                "channel:str*,path:str*",
            ),
            ("set_status", "Set your {svc} status text.", "write", "text:str*"),
            ("list_users", "List members of the {svc} workspace.", "read", "limit:int"),
        ),
        {"discord": {"send_message": "post_message", "get_channel_history": "read_channel"}},
    ),
    "email": _Family(
        "communication",
        (("gmail", "Gmail"), ("outlook", "Outlook"), ("imap", "IMAP mail")),
        (
            (
                "send_email",
                "Send an email from your {svc} account.",
                "write",
                "to:str*,subject:str*,body:str*,cc:str",
            ),
            ("search_email", "Search {svc} messages.", "read", "query:str*,limit:int"),
            ("read_email", "Read one {svc} message.", "read", "message_id:str*"),
            ("list_folders", "List {svc} folders or labels.", "read", ""),
            (
                "move_email",
                "Move a {svc} message to a folder.",
                "write",
                "message_id:str*,folder:str*",
            ),
            ("delete_email", "Delete a {svc} message.", "write!", "message_id:str*"),
            ("create_draft", "Create a {svc} draft.", "write", "to:str*,subject:str*,body:str"),
            ("reply_email", "Reply to a {svc} message.", "write", "message_id:str*,body:str*"),
            ("list_attachments", "List attachments on a {svc} message.", "read", "message_id:str*"),
            ("mark_as_read", "Mark {svc} messages as read.", "write", "message_id:str*"),
        ),
    ),
    "sms": _Family(
        "communication",
        (("twilio", "Twilio"), ("vonage", "Vonage")),
        (
            ("send_sms", "Send an SMS via {svc}.", "write", "to:str*,text:str*"),
            ("list_messages", "List SMS sent and received on {svc}.", "read", "limit:int"),
            (
                "get_message_status",
                "Get delivery status of a {svc} message.",
                "read",
                "message_id:str*",
            ),
            ("list_numbers", "List phone numbers owned on {svc}.", "read", ""),
            ("make_call", "Place a voice call through {svc}.", "execute", "to:str*,script:str*"),
            ("get_call_log", "Get the {svc} call log.", "read", "limit:int"),
            ("send_whatsapp", "Send a WhatsApp message via {svc}.", "write", "to:str*,text:str*"),
            ("list_contacts", "List contacts stored in {svc}.", "read", "limit:int"),
            ("block_number", "Block a number on {svc}.", "write", "number:str*"),
            ("buy_number", "Buy a new phone number on {svc}.", "execute!", "area_code:str*"),
        ),
    ),
    "filesystem": _Family(
        "files",
        (
            ("files", "the local filesystem"),
            ("fs", "the workspace filesystem"),
            ("localfs", "the host filesystem"),
        ),
        (
            (
                "read_file",
                "Read the contents of a file on {svc}.",
                "read",
                "path:str*,encoding:str",
            ),
            ("write_file", "Write content to a file on {svc}.", "write", "path:str*,content:str*"),
            (
                "list_directory",
                "List entries in a directory on {svc}.",
                "read",
                "path:str*,recursive:bool",
            ),
            (
                "search_files",
                "Find files by name pattern on {svc}.",
                "read",
                "path:str*,pattern:str*",
            ),
            ("move_file", "Move or rename a file on {svc}.", "write", "path:str*,destination:str*"),
            ("delete_file", "Delete a file on {svc}.", "write!", "path:str*"),
            (
                "get_file_info",
                "Get size, timestamps and permissions of a file on {svc}.",
                "read",
                "path:str*",
            ),
            ("create_directory", "Create a directory on {svc}.", "write", "path:str*"),
            ("copy_file", "Copy a file on {svc}.", "write", "path:str*,destination:str*"),
            ("tail_file", "Read the last lines of a file on {svc}.", "read", "path:str*,lines:int"),
        ),
        {
            "fs": {
                "read_file": "get_file",
                "write_file": "put_file",
                "list_directory": "list_dir",
                "search_files": "find_files",
                "get_file_info": "stat_file",
            }
        },
    ),
    "cloud_storage": _Family(
        "files",
        (
            ("gdrive", "Google Drive"),
            ("dropbox", "Dropbox"),
            ("onedrive", "OneDrive"),
            ("s3", "Amazon S3"),
        ),
        (
            ("upload_file", "Upload a local file to {svc}.", "write", "path:str*,destination:str*"),
            ("download_file", "Download a file from {svc}.", "read", "file_id:str*,path:str"),
            ("list_files", "List files in a {svc} folder.", "read", "folder:str,limit:int"),
            (
                "search_files",
                "Search {svc} by file name or content.",
                "read",
                "query:str*,limit:int",
            ),
            (
                "share_file",
                "Share a {svc} file with a user.",
                "write",
                "file_id:str*,email:str*,role:str",
            ),
            ("delete_file", "Delete a file from {svc}.", "write!", "file_id:str*"),
            ("get_file_metadata", "Get metadata for a {svc} file.", "read", "file_id:str*"),
            ("create_folder", "Create a folder in {svc}.", "write", "name:str*,parent:str"),
            (
                "move_file",
                "Move a {svc} file to another folder.",
                "write",
                "file_id:str*,destination:str*",
            ),
            ("get_share_link", "Get a shareable link for a {svc} file.", "read", "file_id:str*"),
        ),
        {
            "s3": {
                "list_files": "list_objects",
                "upload_file": "put_object",
                "download_file": "get_object",
                "delete_file": "delete_object",
            }
        },
    ),
    "sql": _Family(
        "databases",
        (
            ("postgres", "PostgreSQL"),
            ("mysql", "MySQL"),
            ("sqlite", "SQLite"),
            ("mssql", "SQL Server"),
        ),
        (
            (
                "run_query",
                "Run a read-only SQL query against {svc}.",
                "read",
                "sql:str*,database:str,limit:int",
            ),
            ("list_tables", "List tables in a {svc} database.", "read", "database:str,schema:str"),
            (
                "describe_table",
                "Describe columns and indexes of a {svc} table.",
                "read",
                "table:str*,database:str",
            ),
            ("list_schemas", "List schemas in a {svc} database.", "read", "database:str"),
            ("explain_query", "Show the {svc} query plan for a statement.", "read", "sql:str*"),
            ("insert_rows", "Insert rows into a {svc} table.", "write", "table:str*,rows:str*"),
            (
                "update_rows",
                "Update rows in a {svc} table.",
                "write!",
                "table:str*,set:str*,where:str*",
            ),
            ("delete_rows", "Delete rows from a {svc} table.", "write!", "table:str*,where:str*"),
            (
                "create_index",
                "Create an index on a {svc} table.",
                "execute",
                "table:str*,columns:str*",
            ),
            ("get_table_stats", "Get row counts and size for a {svc} table.", "read", "table:str*"),
        ),
        {"sqlite": {"run_query": "execute_sql", "describe_table": "table_info"}},
    ),
    "nosql": _Family(
        "databases",
        (
            ("mongodb", "MongoDB"),
            ("redis", "Redis"),
            ("elasticsearch", "Elasticsearch"),
            ("dynamodb", "DynamoDB"),
        ),
        (
            (
                "find_documents",
                "Find documents in a {svc} collection.",
                "read",
                "collection:str*,filter:str,limit:int",
            ),
            (
                "insert_document",
                "Insert a document into {svc}.",
                "write",
                "collection:str*,document:str*",
            ),
            (
                "update_document",
                "Update a document in {svc}.",
                "write",
                "collection:str*,id:str*,patch:str*",
            ),
            (
                "delete_document",
                "Delete a document from {svc}.",
                "write!",
                "collection:str*,id:str*",
            ),
            ("list_collections", "List collections in {svc}.", "read", "database:str"),
            (
                "aggregate",
                "Run an aggregation pipeline in {svc}.",
                "read",
                "collection:str*,pipeline:str*",
            ),
            ("get_key", "Read a value by key from {svc}.", "read", "key:str*"),
            ("set_key", "Write a value by key to {svc}.", "write", "key:str*,value:str*,ttl:int"),
            ("create_index", "Create an index in {svc}.", "execute", "collection:str*,fields:str*"),
            ("get_stats", "Get storage and performance stats from {svc}.", "read", ""),
        ),
    ),
    "warehouse": _Family(
        "databases",
        (("bigquery", "BigQuery"), ("snowflake", "Snowflake"), ("redshift", "Redshift")),
        (
            ("run_query", "Run an analytical SQL query on {svc}.", "read", "sql:str*,limit:int"),
            ("list_datasets", "List {svc} datasets.", "read", "project:str"),
            ("get_table_schema", "Get a {svc} table schema.", "read", "table:str*"),
            (
                "export_results",
                "Export {svc} query results to storage.",
                "execute",
                "job_id:str*,destination:str*",
            ),
            ("list_jobs", "List recent {svc} jobs.", "read", "limit:int"),
            ("cancel_job", "Cancel a running {svc} job.", "execute!", "job_id:str*"),
            ("estimate_query_cost", "Estimate the cost of a {svc} query.", "read", "sql:str*"),
            ("preview_table", "Preview rows of a {svc} table.", "read", "table:str*,limit:int"),
            ("create_view", "Create a {svc} view.", "write", "name:str*,sql:str*"),
            ("load_data", "Load a file into a {svc} table.", "write", "table:str*,uri:str*"),
        ),
    ),
    "docs": _Family(
        "productivity",
        (("notion", "Notion"), ("confluence", "Confluence"), ("gdocs", "Google Docs")),
        (
            ("search_pages", "Search {svc} pages.", "read", "query:str*,limit:int"),
            ("get_page", "Get a {svc} page with its content.", "read", "page_id:str*"),
            ("create_page", "Create a {svc} page.", "write", "title:str*,content:str,parent:str"),
            (
                "update_page",
                "Replace the content of a {svc} page.",
                "write",
                "page_id:str*,content:str*",
            ),
            (
                "append_block",
                "Append content to a {svc} page.",
                "write",
                "page_id:str*,content:str*",
            ),
            ("list_spaces", "List {svc} spaces or workspaces.", "read", ""),
            ("add_page_comment", "Comment on a {svc} page.", "write", "page_id:str*,body:str*"),
            (
                "move_page",
                "Move a {svc} page under another parent.",
                "write",
                "page_id:str*,parent:str*",
            ),
            (
                "export_page",
                "Export a {svc} page as Markdown or PDF.",
                "read",
                "page_id:str*,format:str",
            ),
            ("get_page_history", "List revisions of a {svc} page.", "read", "page_id:str*"),
        ),
    ),
    "calendar": _Family(
        "productivity",
        (("gcal", "Google Calendar"), ("ocal", "Outlook Calendar"), ("caldav", "CalDAV")),
        (
            (
                "list_events",
                "List {svc} events in a time range.",
                "read",
                "start:str*,end:str*,calendar:str",
            ),
            (
                "create_event",
                "Create a {svc} event.",
                "write",
                "title:str*,start:str*,end:str*,attendees:str",
            ),
            ("update_event", "Update a {svc} event.", "write", "event_id:str*,patch:str*"),
            ("delete_event", "Delete a {svc} event.", "write!", "event_id:str*"),
            (
                "find_free_time",
                "Find free slots across {svc} calendars.",
                "read",
                "attendees:str*,duration_minutes:int*",
            ),
            ("list_calendars", "List {svc} calendars.", "read", ""),
            (
                "respond_to_invite",
                "Accept or decline a {svc} invite.",
                "write",
                "event_id:str*,response:str*",
            ),
            ("get_event", "Get a {svc} event.", "read", "event_id:str*"),
            ("search_events", "Search {svc} events by text.", "read", "query:str*,limit:int"),
            (
                "set_reminder",
                "Set a reminder on a {svc} event.",
                "write",
                "event_id:str*,minutes_before:int*",
            ),
        ),
    ),
    "tasks": _Family(
        "productivity",
        (("todoist", "Todoist"), ("trello", "Trello"), ("asana", "Asana")),
        (
            ("list_tasks", "List {svc} tasks.", "read", "project:str,limit:int"),
            ("create_task", "Create a {svc} task.", "write", "title:str*,project:str,due:str"),
            ("complete_task", "Mark a {svc} task complete.", "write", "task_id:str*"),
            ("update_task", "Update a {svc} task.", "write", "task_id:str*,patch:str*"),
            ("delete_task", "Delete a {svc} task.", "write!", "task_id:str*"),
            ("list_projects", "List {svc} projects or boards.", "read", ""),
            ("add_task_comment", "Comment on a {svc} task.", "write", "task_id:str*,body:str*"),
            ("set_due_date", "Set the due date of a {svc} task.", "write", "task_id:str*,due:str*"),
            ("assign_task", "Assign a {svc} task.", "write", "task_id:str*,user:str*"),
            ("search_tasks", "Search {svc} tasks by text.", "read", "query:str*,limit:int"),
        ),
        {"trello": {"create_task": "create_card", "list_tasks": "list_cards"}},
    ),
    "sheets": _Family(
        "productivity",
        (("gsheets", "Google Sheets"), ("airtable", "Airtable"), ("excel", "Excel Online")),
        (
            (
                "read_range",
                "Read a cell range from a {svc} sheet.",
                "read",
                "sheet_id:str*,range:str*",
            ),
            (
                "write_range",
                "Write values to a {svc} range.",
                "write",
                "sheet_id:str*,range:str*,values:str*",
            ),
            ("append_rows", "Append rows to a {svc} sheet.", "write", "sheet_id:str*,rows:str*"),
            ("create_sheet", "Create a {svc} spreadsheet.", "write", "title:str*"),
            ("list_sheets", "List {svc} spreadsheets.", "read", "limit:int"),
            (
                "find_rows",
                "Find rows matching a value in {svc}.",
                "read",
                "sheet_id:str*,query:str*",
            ),
            ("clear_range", "Clear a {svc} range.", "write!", "sheet_id:str*,range:str*"),
            (
                "get_sheet_metadata",
                "Get tabs and dimensions of a {svc} sheet.",
                "read",
                "sheet_id:str*",
            ),
            ("share_sheet", "Share a {svc} sheet.", "write", "sheet_id:str*,email:str*"),
            ("export_sheet", "Export a {svc} sheet as CSV.", "read", "sheet_id:str*"),
        ),
    ),
}

# Interleave families so any prefix of the fleet spans all five domains.
_SERVICE_ORDER: tuple[tuple[str, str, str], ...] = tuple(
    (fam, svc, display)
    for i in range(max(len(f.services) for f in _FAMILIES.values()))
    for fam, f in _FAMILIES.items()
    if i < len(f.services)
    for svc, display in (f.services[i],)
)


def _humanize(name: str) -> str:
    return name.replace("_", " ").capitalize() + "."


def _parse_params(spec: str) -> tuple[ParamSpec, ...]:
    out: list[ParamSpec] = []
    for raw in filter(None, (s.strip() for s in spec.split(","))):
        name, _, typ = raw.partition(":")
        required = typ.endswith("*")
        typ = typ.rstrip("*")
        out.append(
            ParamSpec(name, _PY_TYPES[typ], required, _PARAM_DOCS.get(name, _humanize(name)))
        )
    return tuple(out)


def _family_tools(family_key: str, service: str, display: str) -> list[ToolSpec]:
    fam = _FAMILIES[family_key]
    renames = fam.renames.get(service, {})
    tools: list[ToolSpec] = []
    for name, desc, op, params in fam.templates:
        tools.append(
            ToolSpec(
                name=renames.get(name, name),
                description=desc.format(svc=display),
                params=_parse_params(params),
                domain=fam.domain,
                operation=op.rstrip("!"),
                destructive=op.endswith("!"),
                canonical=f"{family_key}.{name}",
            )
        )
    return tools


def _padded(tools: list[ToolSpec], count: int) -> tuple[ToolSpec, ...]:
    """Exactly ``count`` tools: trim, or extend with deterministic batch variants."""
    if count <= len(tools):
        return tuple(tools[:count])
    out = list(tools)
    round_ = 1
    while len(out) < count:
        for t in tools:
            if len(out) >= count:
                break
            suffix = "" if round_ == 1 else f"_{round_}"
            out.append(
                replace(
                    t,
                    name=f"batch_{t.name}{suffix}",
                    description=f"Batch variant: {t.description} Accepts many inputs at once.",
                    params=(
                        *t.params,
                        ParamSpec("batch_size", "integer", False, "Items per batch."),
                    ),
                    canonical=f"{t.canonical}#batch",
                )
            )
        round_ += 1
    return tuple(out)


def generate_fleet(n_servers: int, *, tools: int | None = None) -> list[ServerSpec]:
    """Return ``n_servers`` server specs.

    ``tools`` (optional) is the exact fleet-wide tool total; it is spread as
    evenly as possible (remainder to the first servers). Without it each
    server carries its family's natural tool set (10-12 tools).
    """
    if n_servers < 1:
        raise ValueError("n_servers must be >= 1")
    if tools is not None and tools < n_servers:
        raise ValueError("tools must be >= n_servers (every server exposes at least one tool)")
    specs: list[ServerSpec] = []
    for i in range(n_servers):
        fam_key, service, display = _SERVICE_ORDER[i % len(_SERVICE_ORDER)]
        instance = i // len(_SERVICE_ORDER)
        name = service if instance == 0 else f"{service}-{instance + 1}"
        base = _family_tools(fam_key, service, display)
        if tools is None:
            chosen = tuple(base)
        else:
            per, extra = divmod(tools, n_servers)
            chosen = _padded(base, per + (1 if i < extra else 0))
        specs.append(
            ServerSpec(
                name=name,
                service=service,
                family=fam_key,
                domain=_FAMILIES[fam_key].domain,
                version="1.0.0",
                tools=chosen,
            )
        )
    return specs


# ---------------------------------------------------------- spec mutations
# Used by discovery tests to simulate a server changing between listings.
def without_tool(spec: ServerSpec, tool: str) -> ServerSpec:
    spec.tool(tool)  # KeyError if absent: a test typo must not pass silently
    return replace(spec, tools=tuple(t for t in spec.tools if t.name != tool))


def with_tool(spec: ServerSpec, tool: ToolSpec) -> ServerSpec:
    return replace(spec, tools=(*spec.tools, tool))


def with_description(spec: ServerSpec, tool: str, description: str) -> ServerSpec:
    old = spec.tool(tool)
    return replace(
        spec,
        tools=tuple(replace(t, description=description) if t is old else t for t in spec.tools),
    )


def with_extra_param(
    spec: ServerSpec, tool: str, param: str, *, required: bool = False
) -> ServerSpec:
    old = spec.tool(tool)
    new = replace(old, params=(*old.params, ParamSpec(param, "string", required, _humanize(param))))
    return replace(spec, tools=tuple(new if t is old else t for t in spec.tools))


def manifest(specs: list[ServerSpec]) -> list[dict[str, Any]]:
    return [asdict(s) for s in specs]


def spec_from_dict(d: dict[str, Any]) -> ServerSpec:
    """Inverse of ``asdict(ServerSpec)`` (used by the stdio entrypoint)."""
    tools = tuple(
        ToolSpec(
            **{k: v for k, v in t.items() if k != "params"},
            params=tuple(ParamSpec(**p) for p in t["params"]),
        )
        for t in d["tools"]
    )
    return ServerSpec(**{k: v for k, v in d.items() if k != "tools"}, tools=tools)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m testbed.fleet", description=__doc__)
    ap.add_argument("--servers", type=int, default=10)
    ap.add_argument("--tools", type=int, default=None)
    args = ap.parse_args(argv)
    json.dump(manifest(generate_fleet(args.servers, tools=args.tools)), sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
