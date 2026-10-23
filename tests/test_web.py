import pytest
from fastapi.testclient import TestClient

from studiradar.config import Config
from studiradar.engine import Engine
from studiradar.web import create_app


@pytest.fixture
def client(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    cfg.app.secret_key = "test-secret"
    cfg.app.password = "pw123"
    cfg.ilias.enabled = False
    app = create_app(cfg, Engine(cfg), start_scheduler=False)
    return TestClient(app)


def test_requires_login(client):
    assert client.get("/api/overview").status_code == 401
    assert client.get("/").status_code == 200
    assert client.get("/sw.js").headers["content-type"].startswith("application/javascript")
    assert client.get("/manifest.webmanifest").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_login_and_tasks(client, monkeypatch):
    monkeypatch.setattr("studiradar.web.time.sleep", lambda s: None)
    assert client.post("/api/login", json={"password": "falsch"}).status_code == 401
    assert client.post("/api/login", json={"password": "pw123"}).status_code == 200
    assert client.get("/api/me").json()["auth"] is True

    r = client.post("/api/tasks", json={"title": "Referat vorbereiten", "due_at": "2030-01-10T14:00", "course": "WI"})
    assert r.status_code == 200
    task = r.json()
    assert task["due_at"].startswith("2030-01-10T14:00")

    r = client.post("/api/tasks", json={"title": "Bücher zurückgeben"})
    assert r.json()["due_at"] is None

    ov = client.get("/api/overview").json()
    assert [d["title"] for d in ov["deadlines"]] == ["Referat vorbereiten"]
    assert [n["title"] for n in ov["notes"]] == ["Bücher zurückgeben"]

    r = client.post(f"/api/items/{task['id']}", json={"done": True})
    assert r.json()["done"] == 1
    r = client.post(f"/api/items/{task['id']}", json={"title": "Referat halten", "due_at": ""})
    assert r.json()["title"] == "Referat halten" and r.json()["due_at"] is None

    assert client.delete(f"/api/tasks/{task['id']}").status_code == 200
    assert client.post("/api/tasks", json={"title": "  "}).status_code == 400


def test_push_endpoints(client):
    client.post("/api/login", json={"password": "pw123"})
    assert len(client.get("/api/push/key").json()["key"]) == 87
    sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "x", "auth": "y"}}
    assert client.post("/api/push/subscribe", json=sub).status_code == 200
    assert client.post("/api/push/subscribe", json={"foo": 1}).status_code == 400
    assert client.post("/api/push/unsubscribe", json={"endpoint": sub["endpoint"]}).status_code == 200


def test_klips_disabled(client):
    client.post("/api/login", json={"password": "pw123"})
    assert client.post("/api/klips/start").status_code == 400
    assert client.post("/api/sync/nope").status_code == 404
