"""Webserver: liefert die PWA aus und stellt die API bereit."""

from __future__ import annotations

import hmac
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from .config import Config
from .dates import find_datetime, iso, now
from .engine import Engine

STATIC = Path(__file__).parent / "static"


class LoginBody(BaseModel):
    password: str


class TaskBody(BaseModel):
    title: str
    due_at: str | None = None
    course: str = ""
    detail: str = ""


class ItemPatch(BaseModel):
    done: bool | None = None
    seen: bool | None = None
    hidden: bool | None = None
    title: str | None = None
    due_at: str | None = None
    course: str | None = None
    detail: str | None = None


def _norm_due(value: str | None) -> str | None:
    """'2026-11-03T23:59' aus dem Formular → ISO mit Zeitzone."""
    if not value:
        return None
    dt = find_datetime(value.replace("T", " "))
    if dt is None:
        raise HTTPException(400, "Datum nicht erkannt")
    return iso(dt)


def create_app(cfg: Config, engine: Engine | None = None, start_scheduler: bool = True) -> FastAPI:
    engine = engine or Engine(cfg)
    db = engine.db
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_scheduler:
            engine.start()
        yield
        engine.stop()

    app = FastAPI(title="StudiRadar", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware, secret_key=cfg.app.secret_key, max_age=365 * 24 * 3600,
        same_site="lax", session_cookie="studiradar",
    )
    app.state.engine = engine

    def auth(request: Request) -> None:
        if not request.session.get("auth"):
            raise HTTPException(401, "Nicht angemeldet")

    # ---------- Seiten & PWA-Dateien ----------
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/sw.js")
    def service_worker():
        return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ---------- Anmeldung ----------
    @app.post("/api/login")
    def login(body: LoginBody, request: Request):
        if not cfg.app.password:
            raise HTTPException(400, "Bitte zuerst in config.toml unter [app] ein password setzen")
        if not hmac.compare_digest(body.password.encode(), cfg.app.password.encode()):
            time.sleep(1.5)
            raise HTTPException(401, "Falsches Passwort")
        request.session["auth"] = True
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        return {"auth": bool(request.session.get("auth"))}

    # ---------- Daten ----------
    @app.get("/api/overview", dependencies=[Depends(auth)])
    def overview():
        return {
            "deadlines": db.deadlines(include_done=True, past_days=2),
            "events": db.events(days=7),
            "feed": db.feed(limit=80),
            "notes": db.tasks_without_due(),
            "unread": db.unread_count(),
            "syncs": db.last_syncs(),
            "klips": engine.klips_state,
            "sources": {
                "ilias": cfg.ilias.enabled,
                "klips": cfg.klips.enabled,
                "ical": [label for _, label, _ in engine.ical_feeds()],
            },
            "now": iso(now()),
        }

    @app.get("/api/feed", dependencies=[Depends(auth)])
    def feed(limit: int = 200):
        return db.feed(limit=min(limit, 500))

    @app.post("/api/items/{item_id}", dependencies=[Depends(auth)])
    def patch_item(item_id: int, body: ItemPatch):
        item = db.item(item_id)
        if not item:
            raise HTTPException(404, "Nicht gefunden")
        fields = body.model_dump(exclude_unset=True)
        if "due_at" in fields:
            fields["due_at"] = _norm_due(fields["due_at"])
        for flag in ("done", "seen", "hidden"):
            if flag in fields and fields[flag] is not None:
                fields[flag] = 1 if fields[flag] else 0
        if item["source"] != "manual":
            # Bei Uni-Einträgen nur Status-Felder änderbar
            fields = {k: v for k, v in fields.items() if k in ("done", "seen", "hidden")}
        return db.update_item(item_id, **fields)

    @app.post("/api/tasks", dependencies=[Depends(auth)])
    def add_task(body: TaskBody):
        title = body.title.strip()
        if not title:
            raise HTTPException(400, "Titel fehlt")
        return db.add_task(title[:300], _norm_due(body.due_at), body.course.strip()[:120],
                           body.detail.strip()[:2000])

    @app.delete("/api/tasks/{item_id}", dependencies=[Depends(auth)])
    def delete_task(item_id: int):
        db.delete_task(item_id)
        return {"ok": True}

    @app.post("/api/seen-all", dependencies=[Depends(auth)])
    def seen_all():
        db.mark_all_seen()
        return {"ok": True}

    # ---------- Abrufe ----------
    @app.post("/api/sync/{source}", dependencies=[Depends(auth)])
    def sync(source: str):
        fn = {"ilias": engine.sync_ilias, "ical": engine.sync_ical}.get(source)
        if fn is None:
            raise HTTPException(404, "Unbekannte Quelle")
        if engine.locks[source].locked():
            return {"status": "busy"}
        threading.Thread(target=fn, daemon=True).start()
        return {"status": "started"}

    @app.get("/api/syncs", dependencies=[Depends(auth)])
    def syncs():
        return {"last": db.last_syncs(),
                "running": [k for k, lock in engine.locks.items() if lock.locked()]}

    @app.post("/api/klips/start", dependencies=[Depends(auth)])
    def klips_start():
        if not cfg.klips.enabled:
            raise HTTPException(400, "KLIPS ist in config.toml deaktiviert")
        started = engine.start_klips_interactive()
        return {"started": started, "state": engine.klips_state}

    @app.get("/api/klips/status", dependencies=[Depends(auth)])
    def klips_status():
        return engine.klips_state

    # ---------- Push ----------
    @app.get("/api/push/key")
    def push_key():
        return {"key": engine.pusher.public_key}

    @app.post("/api/push/subscribe", dependencies=[Depends(auth)])
    async def push_subscribe(request: Request):
        sub = await request.json()
        if not isinstance(sub, dict) or "endpoint" not in sub or "keys" not in sub:
            raise HTTPException(400, "Ungültiges Abo")
        db.add_sub(sub)
        return {"ok": True}

    @app.post("/api/push/unsubscribe", dependencies=[Depends(auth)])
    async def push_unsubscribe(request: Request):
        sub = await request.json()
        db.remove_sub(sub.get("endpoint", ""))
        return {"ok": True}

    @app.post("/api/push/test", dependencies=[Depends(auth)])
    def push_test():
        n = engine.pusher.send("StudiRadar 👋", "Benachrichtigungen funktionieren!", "/", urgent=True)
        return {"sent": n}

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app
