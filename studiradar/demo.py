"""Demo-Modus: App mit Beispieldaten ansehen – ohne Zugangsdaten, ohne Konfiguration."""

from __future__ import annotations

import threading
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .dates import TZ, iso
from .db import Item
from .engine import Engine

DEMO_PASSWORD = "demo"


def _seed(engine: Engine) -> None:
    db = engine.db
    if db.get("demo_seeded"):
        return
    send = engine.pusher.send
    engine.pusher.send = lambda *a, **k: 0  # beim Befüllen keine Push-Nachrichten
    n = datetime.now(TZ)
    at = lambda days, h, m=0: iso((n + timedelta(days=days)).replace(hour=h, minute=m, second=0, microsecond=0))

    engine._store("ilias", [], "ILIAS")       # Ausgangsstand merken
    engine._store("ical:ilias", [], "Kalender")
    engine._store("ilias", [
        Item("ilias", "file:1", "file", "Vorlesung 05 – Datenbanken.pdf", course="Wirtschaftsinformatik II",
             detail="pdf · 2,4 MB · Version: 2", url="https://www.ilias.uni-koeln.de"),
        Item("ilias", "file:2", "file", "Übungsblatt 4.pdf", course="Operations Research",
             detail="Übungsmaterial · pdf", url="https://www.ilias.uni-koeln.de"),
        Item("ilias", "frm:3", "forum", "Diskussionsforum", course="Informationsmanagement",
             detail="Ungelesen: 4", sig="x", url="https://www.ilias.uni-koeln.de"),
        Item("ilias", "exc:4:ass:1", "deadline", "Übungsblatt 3: SQL", course="Wirtschaftsinformatik II",
             due_at=iso(n + timedelta(hours=9)), detail="Übung WI II · nicht abgegeben",
             url="https://www.ilias.uni-koeln.de"),
        Item("ilias", "exc:5:ass:1", "deadline", "Case Study Gruppe 2", course="Informationsmanagement",
             due_at=at(2, 23, 59), url="https://www.ilias.uni-koeln.de"),
        Item("ilias", "tst:6", "test", "Probeklausur online", course="Operations Research",
             due_at=at(12, 23, 59), url="https://www.ilias.uni-koeln.de"),
    ], "ILIAS")
    engine._store("ical:ilias", [
        Item("ical:ilias", "ev1", "event", "Vorlesung Operations Research", course="Operations Research",
             due_at=at(1, 10), detail="bis 11:30 · Hörsaal B", notify=False),
        Item("ical:ilias", "ev2", "event", "Tutorium WI II", course="Wirtschaftsinformatik II",
             due_at=at(2, 14), detail="bis 15:30 · Seminarraum 1.12", notify=False),
        Item("ical:ilias", "ev3", "event", "Vorlesung Informationsmanagement", course="Informationsmanagement",
             due_at=at(3, 12), detail="bis 13:30 · Aula 2", notify=False),
    ], "Kalender")
    db.add_task("Bewerbung Werkstudentenstelle abschicken", at(4, 18), "Privat")
    db.add_task("Bücher in der USB verlängern", None)
    lid = db.log_start("ilias")
    db.log_end(lid, "ok", "Demo-Daten", 6)
    db.set("demo_seeded", "1")
    engine.pusher.send = send


def run_demo(port: int = 8080, open_browser: bool = True) -> None:
    import uvicorn

    from .web import create_app

    data_dir = Path.cwd() / "demo-data"
    cfg = Config(path=data_dir / "config.toml")
    cfg.app.data_dir = str(data_dir)
    cfg.app.password = DEMO_PASSWORD
    cfg.app.host = "127.0.0.1"
    cfg.app.port = port
    cfg.ilias.enabled = False   # Demo ruft nichts bei der Uni ab
    cfg.klips.enabled = False
    key_file = cfg.data_path / "secret.key"
    if not key_file.exists():
        import secrets

        key_file.write_text(secrets.token_hex(32))
    cfg.app.secret_key = key_file.read_text().strip()

    engine = Engine(cfg)
    _seed(engine)
    url = f"http://localhost:{port}/"
    print("\n" + "=" * 52)
    print("  StudiRadar-Demo läuft:", url)
    print(f"  Passwort: {DEMO_PASSWORD}")
    print("  Beenden mit Strg+C")
    print("=" * 52 + "\n")
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    app = create_app(cfg, engine)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
