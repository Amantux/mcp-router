"""Rule-based baseline tool classifier (deterministic, zero-ML).

This is the always-available fallback; the inference track may add a model
classifier behind the same `ToolClassifier` protocol.

It is deliberately CONSERVATIVE: it prefers `unknown` (operation) / `None`
(domain) to a wrong guess. That is safe only because the gateway treats an
`unknown` operation as the MOST restricted class (at least as strict as
`execute`). If that assumption ever changes, this classifier must be revisited:
an `unknown` would then silently widen access.

Operation rules (name first, description only as a fallback):
1. Split the tool name into tokens (snake/kebab/dot/camel case).
2. The LEAD verb is the first token that is a known verb (tokens before it are
   treated as a namespace, e.g. `github_create_issue`).
3. If any LATER token is a verb of a MORE severe class than the lead
   (read < write < execute), refuse to guess -> `unknown`
   (`get_or_delete_user`, `get_run`). Less severe later verbs are ignored
   (`run_query` -> execute).
4. No verb in the name -> look ONLY at the first word of the description
   ("Lists ...", "Creates ..."); a verb later in the prose is not trusted.
5. If the input schema carries an execute-shaped property (command/script/...)
   and the inferred class is read or write -> `unknown`.

Domain rules: count exact keyword-token hits per domain over name +
description tokens; the top domain wins only with a strict margin over the
runner-up. No hits or a tie -> None.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

OPERATIONS = ("read", "write", "execute", "unknown")
_SEVERITY = {"read": 0, "write": 1, "execute": 2}


@dataclass(frozen=True)
class Classification:
    operation: str  # read|write|execute|unknown
    domain: str | None
    capabilities: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)  # human-readable reasons


@runtime_checkable
class ToolClassifier(Protocol):
    name: str

    def classify(
        self, name: str, description: str, input_schema: dict[str, Any]
    ) -> Classification: ...


def _inflect(words: set[str]) -> set[str]:
    out = set(words)
    for w in words:
        out.add(w + "s")
        out.add(w + "es")
    return out


_READ = {
    "get", "list", "search", "read", "fetch", "find", "query", "describe",
    "show", "view", "lookup", "retrieve",
}  # fmt: skip
_WRITE = {
    "create", "update", "send", "write", "add", "set", "post", "put", "insert",
    "edit", "modify", "upload", "rename", "append", "save", "reply", "comment",
}  # fmt: skip
_EXECUTE = {
    "run", "exec", "execute", "delete", "deploy", "remove", "drop", "kill",
    "invoke", "destroy", "purge", "terminate", "eval", "restart", "shutdown",
}  # fmt: skip

# Exact name tokens (no inflection: `runs`/`posts` in a name are nouns).
_NAME_VERBS: dict[str, str] = {
    **{w: "read" for w in _READ},
    **{w: "write" for w in _WRITE},
    **{w: "execute" for w in _EXECUTE},
}
# Description first word: third-person forms allowed ("Lists", "Searches").
_DESC_VERBS: dict[str, str] = {
    **{w: "read" for w in _inflect(_READ)},
    **{w: "write" for w in _inflect(_WRITE)},
    **{w: "execute" for w in _inflect(_EXECUTE)},
}
_EXECUTE_SCHEMA_PROPS = {"command", "cmd", "script", "shell", "code_to_run"}

_DOMAIN_KEYWORDS: dict[str, set[str]] = {
    "development": {
        "github", "gitlab", "git", "repo", "repository", "repositories", "commit",
        "commits", "branch", "branches", "pull", "merge", "issue", "issues", "ci",
        "pipeline", "build", "code", "lint", "pr", "prs",
    },
    "communication": {
        "slack", "email", "emails", "mail", "message", "messages", "chat",
        "channel", "channels", "discord", "sms", "teams", "inbox", "dm",
    },
    "files": {
        "file", "files", "directory", "directories", "folder", "folders",
        "filesystem", "path", "download", "upload", "attachment",
    },
    "databases": {
        "sql", "database", "databases", "db", "table", "tables", "postgres",
        "postgresql", "mysql", "sqlite", "mongo", "mongodb", "redis", "rows",
    },
    "productivity": {
        "calendar", "event", "events", "meeting", "meetings", "todo", "todos",
        "task", "tasks", "note", "notes", "notion", "reminder", "reminders",
        "spreadsheet", "jira",
    },
}  # fmt: skip

_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_WORD = re.compile(r"[A-Za-z0-9]+")


def split_identifier(raw: str) -> list[str]:
    """`github.create-pullRequest` -> ['github', 'create', 'pull', 'request']."""
    tokens: list[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", raw):
        tokens.extend(t.lower() for t in _CAMEL.findall(part))
    return tokens


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


class RuleBasedClassifier:
    name = "rules-v1"

    def classify(self, name: str, description: str, input_schema: dict[str, Any]) -> Classification:
        evidence: list[str] = []
        operation = self._operation(name, description, evidence)
        operation = self._schema_check(operation, input_schema, evidence)
        domain = self._domain(name, description, evidence)
        return Classification(operation=operation, domain=domain, evidence=evidence)

    @staticmethod
    def _operation(name: str, description: str, evidence: list[str]) -> str:
        tokens = split_identifier(name)
        lead_idx = next((i for i, t in enumerate(tokens) if t in _NAME_VERBS), None)
        if lead_idx is not None:
            lead = tokens[lead_idx]
            op = _NAME_VERBS[lead]
            for later in tokens[lead_idx + 1 :]:
                later_op = _NAME_VERBS.get(later)
                if later_op is not None and _SEVERITY[later_op] > _SEVERITY[op]:
                    evidence.append(f"name verbs conflict: '{lead}'={op} then '{later}'={later_op}")
                    return "unknown"
            evidence.append(f"name verb '{lead}' -> {op}")
            return op
        words = _words(description)
        if words and words[0] in _DESC_VERBS:
            op = _DESC_VERBS[words[0]]
            evidence.append(f"description leading verb '{words[0]}' -> {op}")
            return op
        evidence.append("no recognised verb -> unknown")
        return "unknown"

    @staticmethod
    def _schema_check(operation: str, input_schema: dict[str, Any], evidence: list[str]) -> str:
        props = input_schema.get("properties") if isinstance(input_schema, dict) else None
        if not isinstance(props, dict):
            return operation
        hits = sorted(p for p in props if str(p).lower() in _EXECUTE_SCHEMA_PROPS)
        if hits and operation in ("read", "write"):
            evidence.append(f"schema property {hits[0]!r} looks executable -> unknown")
            return "unknown"
        return operation

    @staticmethod
    def _domain(name: str, description: str, evidence: list[str]) -> str | None:
        tokens = split_identifier(name) + _words(description)
        scores: dict[str, int] = {}
        for domain, keywords in _DOMAIN_KEYWORDS.items():
            n = sum(1 for t in tokens if t in keywords)
            if n:
                scores[domain] = n
        if not scores:
            return None
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            evidence.append(f"domain tie {ranked[0][0]}/{ranked[1][0]} -> none")
            return None
        evidence.append(f"domain keywords -> {ranked[0][0]} ({ranked[0][1]} hits)")
        return ranked[0][0]
