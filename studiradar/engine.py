"""Abrufe, Erinnerungen und Zeitplan – das Herz von StudiRadar."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import timedelta

import requests

from .config import Config
from .dates import fmt_relative, iso, now, parse_iso
from .db import DB
from .notify import Pusher
from .sources.common import LoginError, SourceError

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, cfg: Config, debug: bool = False):
        self.cfg = cfg
        self.debug = debug
        self.db = DB(cfg.data_path / "studiradar.db")
        self.pusher = Pusher(
            self.db, cfg.data_path, cfg.app.contact_email,
            quiet=(cfg.notify.quiet_start, cfg.notify.quiet_end) if cfg.notify.quiet_start else None,
        )
        self.locks = {name: threading.Lock() for name in ("ilias", "ical", "klips")}
        self.klips_state: dict = {"state": "idle", "message": "", "code": None, "updated": iso(now())}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- gemeinsame Logik ----------
    def _store(self, source: str, items, label: str, complete: bool = True) -> int:
        baseline = self.db.get(f"baseline:{source}") is None
        res = self.db.upsert_items(source, items, baseline=baseline, mark_gone=complete)
        if baseline:
            self.db.set(f"baseline:{source}", iso(now()))
        else:
            self.pusher.announce(label, res.new, res.changed)
        return len(res.new) + len(res.changed)

    def _run_logged(self, source: str, fn) -> dict:
        lock = self.locks[source.split(":")[0]]
        if not lock.acquire(blocking=False):
            return {"status": "busy", "message": "Läuft bereits"}
        log_id = self.db.log_start(source)
        try:
            count, message = fn()
            status = "warn" if message else "ok"
            self.db.log_end(log_id, status, message, count)
            return {"status": status, "new": count, "message": message}
        except LoginError as exc:
            self.db.log_end(log_id, "error", f"Login: {exc}")
            return {"status": "error", "message": f"Login: {exc}"}
        except SourceError as exc:
            self.db.log_end(log_id, "error", str(exc))
            return {"status": "error", "message": str(exc)}
        except requests.RequestException as exc:
            log.warning("Abruf %s: Verbindungsproblem: %s", source, exc)
            msg = f"Keine Verbindung ({type(exc).__name__}). Internet/Uni-Server prüfen."
            self.db.log_end(log_id, "error", msg)
            return {"status": "error", "message": msg}
        except Exception as exc:  # noqa: BLE001
            log.exception("Abruf %s fehlgeschlagen", source)
            msg = f"Unerwarteter Fehler: {exc}"[:300] + " (Details im Log: journalctl -u studiradar)"
            self.db.log_end(log_id, "error", msg)
            return {"status": "error", "message": msg}
        finally:
            lock.release()

    # ---------- ILIAS ----------
    def sync_ilias(self) -> dict:
        def work():
            from .sources.ilias import IliasClient

            client = IliasClient(self.cfg.ilias, self.cfg.data_path, debug=self.debug)
            items, errors = client.crawl()
            n = self._store("ilias", items, "ILIAS", complete=not errors)
            # Abgegebene Übungsaufgaben automatisch als erledigt markieren
            self.db.x(
                "UPDATE items SET done=1 WHERE source='ilias' AND kind='deadline' AND done=0"
                " AND json_extract(data, '$.submitted')=1"
            )
            if self.cfg.ilias.news_feed_url:
                try:
                    n += self._store("ilias-news", client.news_feed(), "ILIAS")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"News-Feed: {exc}")
            return n, "\n".join(errors[:20])

        if not self.cfg.ilias.enabled:
            return {"status": "off", "message": "ILIAS ist deaktiviert"}
        return self._run_logged("ilias", work)

    # ---------- Kalender-Abos ----------
    def ical_feeds(self) -> list[tuple[str, str, str]]:
        feeds = []
        if self.cfg.ilias.ical_url:
            feeds.append(("ical:ilias", "ILIAS-Kalender", self.cfg.ilias.ical_url))
        if self.cfg.klips.ical_url:
            feeds.append(("ical:klips", "KLIPS-Kalender", self.cfg.klips.ical_url))
        for f in self.cfg.ical:
            if f.url:
                feeds.append((f"ical:{f.name or 'extra'}", f.name or "Kalender", f.url))
        return feeds

    def sync_ical(self) -> dict:
        def work():
            from .sources.feeds import fetch_ical

            total, errors = 0, []
            for source, label, url in self.ical_feeds():
                try:
                    total += self._store(source, fetch_ical(url, source), label)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{label}: {exc}")
            return total, "\n".join(errors)

        if not self.ical_feeds():
            return {"status": "off", "message": "Keine Kalender-Abos eingetragen"}
        return self._run_logged("ical", work)

    # ---------- KLIPS ----------
    def _klips_status(self, state: str, message: str, code: str | None = None) -> None:
        prev_code = self.klips_state.get("code")
        self.klips_state = {"state": state, "message": message, "code": code, "updated": iso(now())}
        if state == "duo_wait" and code and code != prev_code:
            self.pusher.send("KLIPS: Duo bestätigen", f"Gib {code} in Duo Mobile ein", "/#klips",
                             tag="klips-duo", urgent=True)

    def sync_klips(self, interactive: bool = False) -> dict:
        def work():
            from .sources.klips import DuoRequired, KlipsClient

            client = KlipsClient(self.cfg.klips, self.cfg.data_path, self._klips_status, debug=self.debug)
            try:
                items, errors = client.run(interactive=interactive)
            except DuoRequired:
                self._klips_status("needs_user", "Duo-Bestätigung nötig")
                today = now().date().isoformat()
                if self.db.get("klips_reminder_day") != today:
                    self.db.set("klips_reminder_day", today)
                    self.pusher.send("KLIPS wartet auf dich",
                                     "Tippe hier und starte den KLIPS-Abruf (Duo-Bestätigung nötig)",
                                     "/#klips", tag="klips-duo")
                return 0, "Duo-Bestätigung nötig"
            except Exception as exc:
                self._klips_status("error", str(exc))
                raise
            n = self._store("klips", items, "KLIPS", complete=not errors)
            self.db.set("klips_last_ok", iso(now()))
            self._klips_status("done", f"Fertig – {n} neue Einträge")
            return n, "\n".join(errors)

        if not self.cfg.klips.enabled:
            return {"status": "off", "message": "KLIPS ist deaktiviert"}
        return self._run_logged("klips", work)

    def start_klips_interactive(self) -> bool:
        if self.locks["klips"].locked():
            return False
        self._klips_status("starting", "Starte …")
        threading.Thread(target=self.sync_klips, kwargs={"interactive": True}, daemon=True).start()
        return True

    # ---------- Erinnerungen & Zusammenfassung ----------
    def check_reminders(self) -> int:
        hours_list = sorted(set(self.cfg.notify.reminders_hours), reverse=True)
        if not hours_list:
            return 0
        current = now()
        horizon = iso(current + timedelta(hours=hours_list[0]))
        rows = self.db.q(
            "SELECT * FROM items WHERE done=0 AND hidden=0 AND gone=0 AND due_at IS NOT NULL"
            " AND due_at>? AND due_at<=? AND kind IN ('deadline','task','test','exam')",
            (iso(current), horizon),
        )
        due_now = []
        for row in rows:
            try:
                if json.loads(row.get("data") or "{}").get("submitted"):
                    continue
            except ValueError:
                pass
            left = parse_iso(row["due_at"]) - current
            applicable = [h for h in hours_list if left <= timedelta(hours=h)]
            if not applicable:
                continue
            smallest = min(applicable)
            if self.db.reminder_sent(row["id"], smallest):
                continue
            for h in applicable:
                self.db.mark_reminder(row["id"], h)
            due_now.append(row)
        if not due_now:
            return 0
        if len(due_now) == 1:
            r = due_now[0]
            self.pusher.send(
                f"⏰ Frist {fmt_relative(parse_iso(r['due_at']))}",
                r["title"] + (f" · {r['course']}" if r.get("course") else ""),
                f"/#item-{r['id']}", tag=f"rem-{r['id']}",
            )
        else:
            lines = [f"• {fmt_relative(parse_iso(r['due_at']))}: {r['title'][:60]}" for r in due_now[:6]]
            self.pusher.send(f"⏰ {len(due_now)} Fristen stehen an", "\n".join(lines), "/#deadlines")
        return len(due_now)

    def digest(self) -> bool:
        current = now()
        soon = self.db.q(
            "SELECT * FROM items WHERE done=0 AND hidden=0 AND gone=0 AND due_at>? AND due_at<=?"
            " AND kind IN ('deadline','task','test','exam') ORDER BY due_at",
            (iso(current), iso(current + timedelta(days=3))),
        )
        today_events = [e for e in self.db.events(days=1) if parse_iso(e["due_at"]).date() == current.date()]
        unread = self.db.unread_count()
        if not soon and not unread and not today_events:
            return False
        lines = []
        for r in soon[:4]:
            lines.append(f"• {fmt_relative(parse_iso(r['due_at']))}: {r['title'][:55]}")
        if today_events:
            lines.append(f"📅 Heute {len(today_events)} Termin(e), erster {parse_iso(today_events[0]['due_at']).strftime('%H:%M')}: {today_events[0]['title'][:40]}")
        if unread:
            lines.append(f"🆕 {unread} ungelesene Neuigkeiten")
        self.pusher.send("Guten Morgen! Dein Uni-Überblick", "\n".join(lines), "/", tag="digest")
        return True

    # ---------- Zeitplan ----------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _bg(self, fn, *args, **kwargs) -> None:
        threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()

    def _loop(self) -> None:
        time.sleep(5)
        last_run: dict[str, float] = {}
        ilias_every = max(15, self.cfg.ilias.interval_minutes) * 60
        while not self._stop.is_set():
            try:
                t = time.time()
                current = now()
                hm = current.strftime("%H:%M")
                day = current.date().isoformat()

                if self.cfg.ilias.enabled and t - last_run.get("ilias", 0) >= ilias_every:
                    last_run["ilias"] = t
                    self._bg(self.sync_ilias)
                if self.ical_feeds() and t - last_run.get("ical", 0) >= 3600:
                    last_run["ical"] = t
                    self._bg(self.sync_ical)
                if t - last_run.get("reminders", 0) >= 300:
                    last_run["reminders"] = t
                    self.check_reminders()
                if hm == self.cfg.notify.digest_time and self.db.get("digest_day") != day:
                    self.db.set("digest_day", day)
                    self.digest()
                if self.cfg.klips.enabled:
                    for at in self.cfg.klips.auto_times:
                        key = f"klips_auto:{at}"
                        if hm == at and self.db.get(key) != day:
                            self.db.set(key, day)
                            self._bg(self.sync_klips, interactive=False)
                self.pusher.flush_pending()
            except Exception:  # noqa: BLE001
                log.exception("Fehler im Zeitplan")
            self._stop.wait(20)
