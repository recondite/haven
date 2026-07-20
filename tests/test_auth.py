"""Auth middleware: every endpoint 401s without the token when one is set."""
import base64

from fastapi.testclient import TestClient

from haven import config
from haven.main import app

TOKEN = "test-token-123"


def _client(monkeypatch, token):
    monkeypatch.setattr(config, "HAVEN_AUTH_TOKEN", token)
    return TestClient(app)


def test_no_token_configured_allows(monkeypatch):
    # Auth disabled when no token is set (localhost dev default).
    c = _client(monkeypatch, None)
    assert c.get("/favicon.ico").status_code == 200


def test_missing_credentials_401(monkeypatch):
    c = _client(monkeypatch, TOKEN)
    r = c.get("/favicon.ico")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").startswith("Basic")


def test_wrong_token_401(monkeypatch):
    c = _client(monkeypatch, TOKEN)
    assert c.get("/favicon.ico", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_bearer_ok(monkeypatch):
    c = _client(monkeypatch, TOKEN)
    r = c.get("/favicon.ico", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_basic_ok(monkeypatch):
    c = _client(monkeypatch, TOKEN)
    cred = base64.b64encode(f"haven:{TOKEN}".encode()).decode()
    r = c.get("/favicon.ico", headers={"Authorization": f"Basic {cred}"})
    assert r.status_code == 200
