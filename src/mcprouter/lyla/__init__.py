"""Vendored from github.com/Amantux/lyla at commit 013ff01 — MIT License,
Copyright (c) 2026 Alex Moyse (see LICENSE in this directory). The modules in
this package are UNMODIFIED upstream code (relative imports only, stdlib-only);
do not edit them here: change Lyla upstream and re-vendor. Adaptation into the
router's DecisionModel protocol lives in mcprouter.inference.lyla_backend.

Lyla decision model: picks the best MCP tool / Skill for an agent turn.

Implements the pipeline from requirements.md / catalog_schema.md /
algorithm_design.md as a pluggable component:

    hard filter -> coarse retrieval (ANN + lexical, RRF-fused)
    -> fine scoring -> tie-break / abstain -> (optional) LLM arbitration

Usage:

    from lyla import CatalogIndex, DecisionModel, Query, Environment, SessionState

    index = CatalogIndex()
    index.add(entry)                      # incremental, no full rebuild
    model = DecisionModel(index)
    result = model.decide(Query(intent="..."), Environment(), SessionState())
"""

from .schema import CatalogEntry, CostProfile, Reliability, Availability
from .query import Query, Environment, SessionState
from .result import Candidate, Decision
from .index import CatalogIndex
from .pipeline import DecisionModel

__all__ = [
    "CatalogEntry",
    "CostProfile",
    "Reliability",
    "Availability",
    "Query",
    "Environment",
    "SessionState",
    "Candidate",
    "Decision",
    "CatalogIndex",
    "DecisionModel",
]
