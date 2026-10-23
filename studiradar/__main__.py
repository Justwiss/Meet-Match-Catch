"""Kommandozeile: python -m studiradar <befehl>

  serve                 Webserver + Zeitplan starten (Standard)
  sync ilias|ical|klips Einmal abrufen und Ergebnis anzeigen
  login ilias           Nur den ILIAS-Login testen
  push-test             Test-Benachrichtigung an alle Geräte
  digest                Tageszusammenfassung jetzt senden
  list                  Anstehende Fristen in der Konsole zeigen
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from .config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="studiradar", description="Uni-Radar für ILIAS & KLIPS")
    parser.add_argument("command", nargs="?", default="serve",
                        choices=["serve", "sync", "login", "push-test", "digest", "list", "init"])
    parser.add_argument("target", nargs="?", default="")
    parser.add_argument("-c", "--config", default=None, help="Pfad zu config.toml")
    parser.add_argument("--debug", action="store_true", help="HTML-Seiten zur Fehlersuche speichern")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "init":
        target = Path(args.config or "config.toml")
        if target.exists():
            print(f"{target} existiert schon.")
            return 1
        shutil.copy(Path(__file__).parent.parent / "config.example.toml", target)
        target.chmod(0o600)
        print(f"{target} angelegt – bitte Zugangsdaten eintragen.")
        return 0

    cfg = load_config(args.config)
    if cfg.path and not cfg.path.exists():
        print(f"Keine Konfiguration gefunden ({cfg.path}). Erst 'python -m studiradar init' ausführen.")
        return 1

    if args.command == "serve":
        import uvicorn

        from .engine import Engine
        from .web import create_app

        app = create_app(cfg, Engine(cfg, debug=args.debug))
        uvicorn.run(app, host=cfg.app.host, port=cfg.app.port, log_level="info",
                    proxy_headers=True, forwarded_allow_ips="*")
        return 0

    from .engine import Engine

    engine = Engine(cfg, debug=args.debug)

    if args.command == "login":
        from .sources.ilias import IliasClient

        client = IliasClient(cfg.ilias, cfg.data_path, debug=args.debug)
        client.login(force=True)
        courses = client.courses()
        print(f"✅ ILIAS-Login ok. {len(courses)} Kurse/Gruppen gefunden:")
        for c in courses:
            print(f"   • {c.title}")
        return 0

    if args.command == "sync":
        if args.target == "ilias":
            res = engine.sync_ilias()
        elif args.target == "ical":
            res = engine.sync_ical()
        elif args.target == "klips":
            original = engine._klips_status

            def show(state, msg, code=None):
                print(f"[KLIPS] {msg}" + (f"  →  Code für Duo Mobile: {code}" if code else ""))
                original(state, msg, code)

            engine._klips_status = show  # type: ignore[method-assign]
            res = engine.sync_klips(interactive=True)
        else:
            print("Ziel angeben: ilias, ical oder klips")
            return 2
        print(res)
        return 0 if res.get("status") in ("ok", "warn") else 1

    if args.command == "push-test":
        n = engine.pusher.send("StudiRadar 👋", "Benachrichtigungen funktionieren!", urgent=True)
        print(f"An {n} Gerät(e) gesendet.")
        return 0

    if args.command == "digest":
        print("Gesendet." if engine.digest() else "Nichts zu melden.")
        return 0

    if args.command == "list":
        from .dates import fmt_relative, parse_iso

        for r in engine.db.deadlines():
            print(f"{fmt_relative(parse_iso(r['due_at'])):>28}  {r['title']}  [{r['course']}]")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
