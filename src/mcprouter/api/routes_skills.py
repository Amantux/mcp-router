"""Skills REST re-export façade (wave-6 P-207).

The admin read side lives in `routes_skills_admin` (`router`), the agent side
in `routes_skills_agent` (`agent_router`). Include `agent_router` BEFORE
`router` so `/skills/bundle` is not captured by `/skills/{skill_id}`.
"""

from __future__ import annotations

from mcprouter.api.routes_skills_admin import (
    _factory,
    _get,
    _summary,
    get_body,
    get_skill,
    get_versions,
    list_skills,
    patch_classification,
    router,
)
from mcprouter.api.routes_skills_agent import (
    _ERRORS,
    _INTERNAL,
    _NOT_FOUND_CODES,
    ADMIN_NEEDS_AGENT_SKILL,
    SKIPPED_HEADER_MAX,
    UNKNOWN_SKILL,
    ActivateIn,
    ActivateOut,
    SkillResourceOut,
    _acting_agent,
    _AgentQ,
    _call,
    _exposure,
    _http_error,
    _SkillId,
    _Wire,
    activate_skill,
    agent_router,
    read_skill_resource,
    routed_skill_ids,
    skills_bundle,
    skipped_header,
)
from mcprouter.models import SKILL_ID_PREFIX

__all__ = [
    "_acting_agent",
    "_AgentQ",
    "_call",
    "_ERRORS",
    "_exposure",
    "_factory",
    "_get",
    "_http_error",
    "_INTERNAL",
    "_NOT_FOUND_CODES",
    "_SkillId",
    "_summary",
    "_Wire",
    "activate_skill",
    "ActivateIn",
    "ActivateOut",
    "ADMIN_NEEDS_AGENT_SKILL",
    "agent_router",
    "get_body",
    "get_skill",
    "get_versions",
    "list_skills",
    "patch_classification",
    "read_skill_resource",
    "routed_skill_ids",
    "router",
    "SKILL_ID_PREFIX",
    "SkillResourceOut",
    "skills_bundle",
    "skipped_header",
    "SKIPPED_HEADER_MAX",
    "UNKNOWN_SKILL",
]
