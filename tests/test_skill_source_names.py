"""Skill source names: no "/", whitespace or control chars (item A3; 422 via FastAPI)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcprouter.api.routes_skill_sources import SourceIn, SourcePatch

BAD = ["a/b", "/abs", "a b", " lead", "a\n", "a\tb", "a\x00b", ".hidden", "-x", "a" * 121, ""]
GOOD = ["anthropic-skills", "team.skills_v2", "A", "a" * 120]


@pytest.mark.parametrize("name", BAD)
def test_bad_names_rejected_on_create_and_patch(name: str) -> None:
    with pytest.raises(ValidationError):
        SourceIn.model_validate({"name": name, "kind": "directory", "location": "/x"})
    with pytest.raises(ValidationError):
        SourcePatch.model_validate({"name": name})


@pytest.mark.parametrize("name", GOOD)
def test_good_names_accepted(name: str) -> None:
    assert SourceIn.model_validate({"name": name, "kind": "git", "location": "x"}).name == name
    assert SourcePatch.model_validate({"name": name}).name == name
