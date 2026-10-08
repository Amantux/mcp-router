"""Scaffold smoke: contracts import, app boots against the db."""

from fastapi.testclient import TestClient

from mcprouter import interfaces, models
from mcprouter.api.app import create_app
from mcprouter.settings import Settings

from .conftest import TEST_DB_URL, requires_db


def test_contracts_import() -> None:
    assert models.EMBEDDING_DIM == 384
    assert hasattr(interfaces, "DecisionModel")


@requires_db
def test_app_boots_and_healthz() -> None:
    app = create_app(Settings(database_url=TEST_DB_URL), env={})
    client = TestClient(app)
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/metrics").status_code == 200
