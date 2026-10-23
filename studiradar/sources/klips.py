"""KLIPS 2.0 (CAMPUSonline) mit Shibboleth + Cisco Duo.

KLIPS verlangt seit Dezember 2025 eine Duo-Bestätigung. Das lässt sich nicht
voll automatisieren (und soll es auch nicht). Ablauf deshalb:

1. Ein Abruf startet einen Headless-Chromium mit eigenem, dauerhaftem Profil.
2. Shibboleth-Login wird automatisch ausgefüllt.
3. Erscheint Duo, wird die angezeigte Nummer per Push an dein Handy geschickt.
   Du gibst sie in Duo Mobile ein. "Ja, das ist mein Gerät" wird automatisch
   angeklickt, damit Duo das Profil ~9 Stunden lang merkt.
4. Danach werden die konfigurierten Seiten (Noten, Prüfungen, ...) gelesen
   und mit dem letzten Stand verglichen.

Stille, automatische Abrufe (ohne dich) klappen nur, solange Duo das Profil
noch merkt. Sonst gibt es eine Erinnerung, den Abruf in der App zu starten.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import time as dtime
from pathlib import Path
from typing import Callable

from ..config import KlipsConfig
from ..dates import find_datetime, iso
from ..db import Item
from .common import LoginError, SourceError, clean, slug

log = logging.getLogger(__name__)

StatusCallback = Callable[[str, str, str | None], None]

NOISE_RE = re.compile(
    r"zuletzt|aktualisiert|sitzung|session|cookie|datenschutz|impressum|barriere|hilfe|"
    r"abmelden|logout|suche|filter|seite \d+ von|einträge pro seite|keine einträge|"
    r"^(?:mehr|weniger|zurück|weiter|ok|schließen)$",
    re.I,
)
ROW_SELECTORS = "tr, [role='row'], mat-row, .coTableR, li.list-group-item, .ca-list-item, ca-list-item"


class DuoRequired(LoginError):
    """Duo-Bestätigung nötig, aber Abruf läuft ohne dich (still)."""


class KlipsClient:
    def __init__(self, cfg: KlipsConfig, data_path: Path, on_status: StatusCallback | None = None,
                 debug: bool = False):
        self.cfg = cfg
        self.data_path = data_path
        self.profile_dir = data_path / "klips_profile"
        self.on_status = on_status or (lambda state, msg, code=None: None)
        self.debug = debug
        self._step = 0

    def _status(self, state: str, msg: str, code: str | None = None) -> None:
        log.info("KLIPS: %s – %s %s", state, msg, code or "")
        self.on_status(state, msg, code)

    def _dump(self, page, name: str) -> None:
        if not self.debug:
            return
        d = self.data_path / "debug" / "klips"
        d.mkdir(parents=True, exist_ok=True)
        self._step += 1
        base = d / f"{self._step:03d}_{slug(name)[:50]}"
        try:
            base.with_suffix(".html").write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        except Exception:  # noqa: BLE001
            pass

    # ---------- Hauptablauf ----------
    def run(self, interactive: bool) -> tuple[list[Item], list[str]]:
        if not self.cfg.username or not self.cfg.password:
            raise LoginError("KLIPS-Benutzername/Passwort fehlen in config.toml")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SourceError(
                "Playwright ist nicht installiert (pip install playwright) – nötig für KLIPS"
            ) from exc

        items: list[Item] = []
        errors: list[str] = []
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            launch = dict(
                user_data_dir=str(self.profile_dir),
                headless=self.cfg.headless,
                locale="de-DE",
                timezone_id="Europe/Berlin",
                viewport={"width": 1280, "height": 900},
                args=["--disable-dev-shm-usage", "--no-first-run"],
            )
            if self.cfg.chromium_path:
                launch["executable_path"] = self.cfg.chromium_path
            ctx = pw.chromium.launch_persistent_context(**launch)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.set_default_timeout(30_000)
                self._status("login", "Melde bei KLIPS an …")
                page.goto(self.cfg.start_url, wait_until="domcontentloaded")
                self._ensure_login(page, interactive)
                self._status("syncing", "Lese KLIPS-Seiten …")
                for name, url in self.cfg.pages.items():
                    try:
                        items.extend(self._read_page(page, name, url))
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{name}: {exc}")
                        self._dump(page, f"error_{name}")
            finally:
                ctx.close()
        return items, errors

    # ---------- Login ----------
    def _logged_in(self, page) -> bool:
        if "klips2" not in page.url or re.search(r"login|shibboleth|duo", page.url, re.I):
            return False
        try:
            return page.locator("text=/Abmelden|Logout|Sign out|Log out/i").count() > 0
        except Exception:  # noqa: BLE001
            return False

    def _ensure_login(self, page, interactive: bool) -> None:
        deadline = time.time() + self.cfg.duo_timeout_seconds + 90
        filled_password = 0
        clicked_login = 0
        duo_announced = False
        while time.time() < deadline:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:  # noqa: BLE001
                pass
            if self._logged_in(page):
                self._status("syncing", "Angemeldet")
                self._dump(page, "logged_in")
                return
            url = page.url

            if "duosecurity" in url or page.locator("iframe#duo_iframe").count() > 0:
                if self._click_trust(page):
                    time.sleep(2)
                    continue
                if not interactive:
                    self._dump(page, "duo_required")
                    raise DuoRequired("Duo-Bestätigung nötig – bitte Abruf in der App starten")
                code = self._duo_code(page)
                self._duo_old_prompt(page)
                if not duo_announced or code:
                    self._status("duo_wait", "Bitte in Duo Mobile bestätigen", code)
                    duo_announced = True
                self._dump(page, "duo")
                time.sleep(2)
                continue

            pw_field = page.locator("input[type=password]:visible")
            if pw_field.count() > 0:
                if filled_password >= 2:
                    msg = self._error_text(page)
                    self._dump(page, "login_failed")
                    raise LoginError(msg or "KLIPS-Anmeldung fehlgeschlagen (Zugangsdaten prüfen)")
                user_field = page.locator(
                    "input[name=j_username]:visible, input[type=email]:visible, "
                    "input[type=text]:visible, input[name*=user i]:visible"
                ).first
                user_field.fill(self.cfg.username)
                pw_field.first.fill(self.cfg.password)
                self._dump(page, "login_form")
                submit = page.locator(
                    "button[type=submit]:visible, input[type=submit]:visible, "
                    "button[name=_eventId_proceed]:visible"
                ).first
                if submit.count():
                    submit.click()
                else:
                    pw_field.first.press("Enter")
                filled_password += 1
                time.sleep(2)
                continue

            if "klips2" in url and clicked_login < 3:
                btn = page.locator(
                    "a:visible, button:visible",
                    has_text=re.compile(r"anmelden|login|shibboleth|uni-account|single sign", re.I),
                ).first
                if btn.count():
                    btn.click()
                    clicked_login += 1
                    time.sleep(2)
                    continue
            time.sleep(1.5)
        self._dump(page, "timeout")
        raise LoginError("KLIPS-Anmeldung: Zeitüberschreitung (Duo nicht bestätigt?)")

    @staticmethod
    def _error_text(page) -> str:
        for sel in (".output--error", ".alert-danger", ".form-error", ".error", "#errorMessage"):
            loc = page.locator(sel)
            if loc.count():
                return clean(loc.first.inner_text())[:200]
        return ""

    @staticmethod
    def _click_trust(page) -> bool:
        """Duo: 'Ja, das ist mein Gerät' – damit Duo den Browser ~9 h merkt."""
        loc = page.locator(
            "#trust-browser-button, button:has-text('Yes, this is my device'), "
            "button:has-text('Ja, das ist mein Gerät'), button:has-text('Ja, dies ist mein Gerät')"
        )
        if loc.count() and loc.first.is_visible():
            loc.first.click()
            return True
        return False

    @staticmethod
    def _duo_code(page) -> str | None:
        for sel in (".verification-code", "[data-testid='verification-code']", ".verification-code-container"):
            loc = page.locator(sel)
            if loc.count():
                m = re.search(r"\d{2,6}", loc.first.inner_text())
                if m:
                    return m.group(0)
        try:
            text = page.inner_text("body")
        except Exception:  # noqa: BLE001
            return None
        m = re.search(r"(?:code|nummer|number)[^0-9]{0,40}(\d{2,6})", text, re.I)
        return m.group(1) if m else None

    @staticmethod
    def _duo_old_prompt(page) -> None:
        """Ältere Duo-Oberfläche im iframe: '9 Stunden merken' + Push anfordern."""
        if page.locator("iframe#duo_iframe").count() == 0:
            return
        frame = page.frame_locator("iframe#duo_iframe")
        try:
            remember = frame.locator("input[name=dampen_choice]")
            if remember.count() and not remember.first.is_checked():
                remember.first.check()
            push = frame.locator("button:has-text('Push')")
            if push.count():
                push.first.click()
        except Exception:  # noqa: BLE001
            pass

    # ---------- Seiten lesen ----------
    def _read_page(self, page, name: str, url: str) -> list[Item]:
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
        self._dump(page, f"page_{name}")
        rows = []
        try:
            for el in page.locator(ROW_SELECTORS).all()[:400]:
                t = clean(el.inner_text())
                if t:
                    rows.append(t)
        except Exception:  # noqa: BLE001
            rows = []
        if len(rows) < 2:
            rows = [clean(line) for line in page.inner_text("body").splitlines()]
        return rows_to_items(name, rows, url)


def rows_to_items(name: str, rows: list[str], url: str) -> list[Item]:
    """Tabellenzeilen einer KLIPS-Seite in Einträge umwandeln."""
    items: list[Item] = []
    lname = name.lower()
    kind = "grade" if re.search(r"note|leistung|grade|achievement", lname) else (
        "exam" if re.search(r"prüf|exam", lname) else "klips"
    )
    seen: set[str] = set()
    for row in rows:
        if len(row) < 8 or len(row) > 600 or NOISE_RE.search(row) or row in seen:
            continue
        seen.add(row)
        ext = f"{slug(name)}:{hashlib.sha1(row.encode()).hexdigest()[:16]}"
        due = None
        row_kind = kind
        title = row[:160]
        if kind == "exam":
            m = re.search(r"(?:anmeld\w*|registrier\w*)[^0-9]{0,30}bis[^0-9]{0,10}(.{0,40})", row, re.I)
            if m and find_datetime(m.group(1)):
                due = find_datetime(m.group(1))
                row_kind = "deadline"
                title = f"Prüfungsanmeldung: {row[:140]}"
            else:
                due = find_datetime(row, default_time=dtime(0, 0))
        items.append(Item(
            source="klips", ext_id=ext, kind=row_kind, title=title, course=name,
            url=url, detail=row if len(row) > 160 else "", due_at=iso(due),
        ))
    return items
