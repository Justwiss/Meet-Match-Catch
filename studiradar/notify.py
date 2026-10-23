"""Web-Push-Benachrichtigungen an die installierte PWA."""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .dates import now
from .db import DB

log = logging.getLogger(__name__)

KIND_LABEL = {
    "file": "Neue Datei", "deadline": "Neue Frist", "exercise": "Neue Übung", "test": "Neuer Test",
    "forum": "Neue Forenbeiträge", "news": "Neuigkeit", "grade": "Neu in KLIPS (Noten)",
    "exam": "Prüfung", "task": "Aufgabe", "webr": "Neuer Link", "xvid": "Neues Video",
    "xopc": "Neues Video", "wiki": "Wiki", "copa": "Neue Seite",
}
CHANGED_LABEL = {
    "file": "Datei aktualisiert", "forum": "Neue Forenbeiträge", "deadline": "Frist geändert",
    "test": "Test geändert",
}


class Pusher:
    def __init__(self, db: DB, data_path: Path, contact: str, quiet: tuple[str, str] | None = None):
        self.db = db
        self.key_file = data_path / "vapid_private.pem"
        self.contact = contact if contact.startswith("mailto:") else f"mailto:{contact}"
        self.quiet = quiet
        self._ensure_keys()

    def _ensure_keys(self) -> None:
        if self.key_file.exists():
            return
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        self.key_file.write_bytes(pem)
        self.key_file.chmod(0o600)

    @property
    def public_key(self) -> str:
        key = serialization.load_pem_private_key(self.key_file.read_bytes(), password=None)
        raw = key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def in_quiet_hours(self, at: datetime | None = None) -> bool:
        if not self.quiet:
            return False
        at = at or now()
        start, end = self.quiet
        hm = at.strftime("%H:%M")
        if start <= end:
            return start <= hm < end
        return hm >= start or hm < end

    def send(self, title: str, body: str, url: str = "/", tag: str | None = None,
             urgent: bool = False) -> int:
        """An alle registrierten Geräte senden. Gibt Anzahl erfolgreicher Zustellungen zurück."""
        if not urgent and self.in_quiet_hours():
            log.info("Ruhezeit – Push zurückgehalten: %s", title)
            pending = json.loads(self.db.get("pending_push", "[]") or "[]")
            pending.append({"title": title, "body": body, "url": url})
            self.db.set("pending_push", json.dumps(pending[-20:]))
            return 0
        from pywebpush import WebPushException, webpush

        payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
        ok = 0
        for sub in self.db.subs():
            try:
                webpush(
                    subscription_info=sub,
                    data=payload,
                    vapid_private_key=str(self.key_file),
                    vapid_claims={"sub": self.contact},
                    ttl=24 * 3600,
                    headers={"Urgency": "high" if urgent else "normal"},
                )
                ok += 1
            except WebPushException as exc:
                status = getattr(exc.response, "status_code", None)
                if status in (404, 410):
                    log.info("Push-Abo abgelaufen, entferne es")
                    self.db.remove_sub(sub["endpoint"])
                else:
                    log.warning("Push fehlgeschlagen: %s", exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("Push fehlgeschlagen: %s", exc)
        return ok

    def flush_pending(self) -> None:
        """Nach der Ruhezeit gesammelte Nachrichten als eine zusammenfassen."""
        if self.in_quiet_hours():
            return
        pending = json.loads(self.db.get("pending_push", "[]") or "[]")
        if not pending:
            return
        self.db.set("pending_push", "[]")
        if len(pending) == 1:
            p = pending[0]
            self.send(p["title"], p["body"], p.get("url", "/"))
        else:
            body = "\n".join(f"• {p['title']}: {p['body']}"[:120] for p in pending[:6])
            self.send(f"{len(pending)} Nachrichten aus der Nacht", body, "/#updates")

    def announce(self, source_label: str, new: list[dict], changed: list[dict]) -> None:
        """Neue/geänderte Einträge eines Abrufs als eine Nachricht melden."""
        entries = [(KIND_LABEL.get(i["kind"], "Neu"), i) for i in new]
        entries += [(CHANGED_LABEL.get(i["kind"], "Geändert"), i) for i in changed]
        if not entries:
            return
        if len(entries) == 1:
            label, it = entries[0]
            title = f"{label}" + (f" · {it['course']}" if it.get("course") else "")
            body = it["title"]
            if it.get("due_at"):
                from .dates import fmt_relative, parse_iso

                body += f" (bis {fmt_relative(parse_iso(it['due_at']))})"
            self.send(title, body, f"/#item-{it['id']}", tag=f"item-{it['id']}")
            return
        lines = []
        for label, it in entries[:5]:
            course = f" [{it['course'][:25]}]" if it.get("course") else ""
            lines.append(f"• {it['title'][:60]}{course}")
        if len(entries) > 5:
            lines.append(f"… und {len(entries) - 5} weitere")
        self.send(f"{len(entries)} Neuigkeiten aus {source_label}", "\n".join(lines), "/#updates",
                  tag=f"batch-{source_label}")
