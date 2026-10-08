"""422 bodies never echo question keys; they name the question by index."""

from __future__ import annotations

import pytest

from mcprouter.inference import serve
from tests.edge_app_helpers import AUTH, PATH, edge_client


def test_huge_key_is_422_without_echo() -> None:
    key = "k" * 50_000
    body = {"state": "s", "questions": {key: {"type": "noul", "instructions": "x"}}}
    with edge_client() as (_, client):
        r = client.post(PATH, json=body, headers=AUTH)
    assert r.status_code == 422
    assert key[:64] not in r.text
    assert "question 0" in r.json()["detail"]


@pytest.mark.parametrize(
    "q",
    [
        "not-an-object",
        {"type": "noul", "instructions": ""},
        {"type": "bogus", "instructions": "x"},
        {"type": "choice", "instructions": "x", "criteria": ["a"]},
    ],
)
def test_every_error_reports_index_not_key(q: object) -> None:
    with pytest.raises(serve.SystemOneRequestError) as exc:
        serve.parse_questions({"ok_key": {"type": "noul", "instructions": "x"}, "SECRETKEY": q})
    assert "SECRETKEY" not in str(exc.value)
    assert str(exc.value).startswith("question 1:")
