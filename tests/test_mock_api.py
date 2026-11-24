from fastapi.testclient import TestClient

from mock_api.server import app


def test_health() -> None:
    assert TestClient(app).get("/health").json() == {"status": "ok"}
