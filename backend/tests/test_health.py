from fastapi.testclient import TestClient

from titan.app import create_app

# Nothing listens on port 1, so the connection is refused at once.
UNREACHABLE = "postgresql+psycopg://titan:x@127.0.0.1:1/titan"


def test_health_ok_when_database_answers(database_url: str, key_file: str) -> None:
    with TestClient(create_app(database_url, key_file)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_503_when_database_is_down(key_file: str) -> None:
    with TestClient(create_app(UNREACHABLE, key_file)) as client:
        response = client.get("/health")
    assert response.status_code == 503
