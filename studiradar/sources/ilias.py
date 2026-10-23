"""ILIAS-Abruf: Login, Kurse durchsuchen, neue Dateien, Übungen, Tests, Foren.

Bewusst ohne Browser (nur requests + BeautifulSoup), damit es auf einem
Raspberry Pi 3B+ flott läuft. Der Parser ist absichtlich tolerant gebaut,
weil ILIAS-Versionen (7/8/9/10) leicht unterschiedliches HTML erzeugen.
"""

from __future__ import annotations

import logging
import pickle
import re
import time
from datetime import time as dtime
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from ..config import IliasConfig
from ..dates import find_datetime, iso
from ..db import Item
from .common import (
    LoginError,
    SourceError,
    clean,
    fill_login_form,
    find_login_form,
    follow_autosubmit,
    new_session,
    slug,
)

log = logging.getLogger(__name__)

# ILIAS-Objekttypen → Anzeigename
TYPE_NAMES = {
    "crs": "Kurs", "grp": "Gruppe", "fold": "Ordner", "file": "Datei", "exc": "Übung",
    "tst": "Test", "frm": "Forum", "sess": "Sitzung", "webr": "Weblink", "wiki": "Wiki",
    "lm": "Lernmodul", "htlm": "Lernmodul", "sahs": "Lernmodul", "copa": "Inhaltsseite",
    "svy": "Umfrage", "blog": "Blog", "dcl": "Datensammlung", "mcst": "Mediacast",
    "xvid": "Video", "xopc": "Video", "bibl": "Bibliografie", "iass": "Bewertung",
    "lso": "Lernsequenz", "itgr": "Gruppierung", "book": "Buchung", "cat": "Kategorie",
    "glo": "Glossar", "qpl": "Fragenpool", "prtt": "Portfolio", "cld": "Cloud",
    "xlvo": "Live-Voting", "xmst": "Video", "xsrl": "Lernpfad",
}

GUI_TYPES = {
    "ilobjcoursegui": "crs", "ilobjgroupgui": "grp", "ilobjfoldergui": "fold",
    "ilobjexercisegui": "exc", "ilobjfilegui": "file", "ilobjtestgui": "tst",
    "ilobjforumgui": "frm", "ilobjsessiongui": "sess", "ilobjlinkresourcegui": "webr",
    "ilobjwikigui": "wiki", "ilobjsurveygui": "svy", "ilobjcontentpagegui": "copa",
    "ilobjitemgroupgui": "itgr", "ilobjlearningsequencegui": "lso",
}

# Bereiche der Seite, die nicht zum Inhalt gehören (Menüs, Brotkrumen, ...)
SKIP_CLASS_RE = re.compile(
    r"il-mainbar|il-maincontrols|breadcrumb|ilLocator|il-metabar|ilTopBar|il-footer|"
    r"dropdown-menu|il_adv_sel|ilAdvSel|il-item-actions|ilMainMenu|il-mainbar-slates|"
    r"ilFooter|ilTabs|nav-tabs|ilSubTabs|il-viewcontrol|ilHeaderDesc|il-page-header",
    re.IGNORECASE,
)
ITEM_CLASS_RE = re.compile(
    r"\bil_ContainerListItem\b|\bilContainerListItemOuter\b|\bil-item\b|\bil-std-item\b|"
    r"\bilObjListRow\b|\bil-card\b",
)
DEADLINE_LABEL_RE = re.compile(
    r"Abgabetermin|Abgabefrist|Ende der Abgabefrist|Ende der Bearbeitungszeit|Bearbeitungsende|"
    r"Endet am|Fällig am|Submission Deadline|Deadline|Due Date|Ends on|Ende(?=\s*:)",
    re.IGNORECASE,
)
SUBMITTED_RE = re.compile(
    r"abgegeben am|eingereicht am|abgabe erfolgt|submitted on|eingereichte dateien|"
    r"hochgeladene dateien\s*:?\s*[1-9]|bereits abgegeben",
    re.IGNORECASE,
)
NOT_SUBMITTED_RE = re.compile(r"nicht abgegeben|keine abgabe|not submitted|noch nicht", re.IGNORECASE)


@dataclass
class ObjLink:
    type: str
    ref_id: str
    title: str
    url: str
    props: list[str] = field(default_factory=list)
    description: str = ""

    @property
    def props_text(self) -> str:
        return " · ".join(self.props)


def parse_obj_link(href: str) -> tuple[str, str] | None:
    """ILIAS-Link → (Typ, ref_id). Kennt goto-Links und ilias.php-Links."""
    if not href or href.startswith(("javascript:", "#", "mailto:")):
        return None
    # goto_uk_crs_12345.html, goto_uk_file_123_download.html
    m = re.search(r"goto_[^_/]+_([a-z]+)_(\d+)(?:_[a-z]+)?\.html", href)
    if m:
        return m.group(1), m.group(2)
    # goto.php/crs/123 (ILIAS 10)
    m = re.search(r"goto\.php/([a-z]+)/(\d+)", href)
    if m:
        return m.group(1), m.group(2)
    parsed = urlparse(href)
    qs = parse_qs(parsed.query)
    # goto.php?target=crs_123
    target = qs.get("target", [""])[0]
    m = re.match(r"([a-z]+)_(\d+)", target)
    if m:
        return m.group(1), m.group(2)
    ref = qs.get("ref_id", [""])[0]
    if ref.isdigit():
        cls = (qs.get("cmdClass", [""])[0] or "").lower()
        node_type = GUI_TYPES.get(cls)
        if node_type:
            return node_type, ref
    return None


def _skip_anchor(a: Tag) -> bool:
    for parent in a.parents:
        if parent.name in ("nav", "header", "footer"):
            return True
        cls = " ".join(parent.get("class", []) or [])
        if cls and SKIP_CLASS_RE.search(cls):
            return True
        pid = parent.get("id") or ""
        if pid and SKIP_CLASS_RE.search(pid):
            return True
    return False


def _item_container(a: Tag) -> Tag:
    node: Tag = a
    for _ in range(10):
        parent = node.parent
        if parent is None or not isinstance(parent, Tag):
            break
        node = parent
        cls = " ".join(node.get("class", []) or [])
        if ITEM_CLASS_RE.search(cls) or node.name == "tr":
            return node
    return a.parent if isinstance(a.parent, Tag) else a


def _props(box: Tag, title: str) -> tuple[list[str], str]:
    props: list[str] = []
    for el in box.select(".il_ItemProperty, .il_ItemAlertProperty, .il_ItemProperties span"):
        t = clean(el.get_text(" "))
        if t and t not in props:
            props.append(t)
    # UI-Framework (ILIAS 8+): Name/Wert-Paare
    names = box.select(".il-item-property-name")
    values = box.select(".il-item-property-value")
    for n, v in zip(names, values):
        t = clean(f"{n.get_text(' ')}: {v.get_text(' ')}")
        if t and t not in props:
            props.append(t)
    desc_el = box.select_one(".il_Description, .il-item-description, .ilListItemSection.il_Description")
    desc = clean(desc_el.get_text(" ")) if desc_el else ""
    if not props:
        # Fallback: Text des Containers ohne den Titel
        text = clean(box.get_text(" "))
        text = text.replace(title, "", 1).strip(" ·-")
        if desc:
            text = text.replace(desc, "", 1).strip(" ·-")
        if text and len(text) < 300:
            props = [text]
    return props, desc


def container_items(html: str, page_url: str, own_ref: str | None = None) -> list[ObjLink]:
    """Alle ILIAS-Objekte auf einer Container-Seite (Kurs, Ordner, ...) finden."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#il_center_col, main, .il-layout-page-content, #mainspacekeeper") or soup
    found: dict[str, ObjLink] = {}
    for a in root.find_all("a", href=True):
        parsed = parse_obj_link(a["href"])
        if not parsed:
            continue
        typ, ref = parsed
        if ref == own_ref or typ == "cat":
            continue
        title = clean(a.get_text(" "))
        if not title or len(title) > 250 or _skip_anchor(a):
            continue
        key = f"{typ}:{ref}"
        if key in found:
            continue
        box = _item_container(a)
        props, desc = _props(box, title)
        found[key] = ObjLink(typ, ref, title, urljoin(page_url, a["href"]), props, desc)
    return list(found.values())


def membership_links(html: str, page_url: str) -> list[ObjLink]:
    """Kurse und Gruppen von der Dashboard-/Mitgliedschaftsseite."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, ObjLink] = {}
    for a in soup.find_all("a", href=True):
        parsed = parse_obj_link(a["href"])
        if not parsed or parsed[0] not in ("crs", "grp"):
            continue
        title = clean(a.get_text(" "))
        if not title or len(title) > 250:
            continue
        # Hauptmenü-Einträge wie "Zuletzt besucht" überspringen
        if _skip_anchor(a) and not a.find_parent(class_=ITEM_CLASS_RE):
            continue
        key = f"{parsed[0]}:{parsed[1]}"
        out.setdefault(key, ObjLink(parsed[0], parsed[1], title, urljoin(page_url, a["href"])))
    return list(out.values())


@dataclass
class Assignment:
    title: str
    deadline: str | None
    submitted: bool | None
    ass_id: str | None
    text: str


def _label_count(el: Tag) -> int:
    return len(DEADLINE_LABEL_RE.findall(el.get_text(" ")))


def exercise_assignments(html: str) -> list[Assignment]:
    """Aufgaben einer ILIAS-Übung mit Abgabetermin erkennen.

    Vorgehen: zu jedem Vorkommen eines Frist-Labels wird der größte umgebende
    Block gesucht, der nur dieses eine Label enthält – das ist die Aufgabe.
    """
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#il_center_col, main, .il-layout-page-content") or soup
    results: list[Assignment] = []
    seen_blocks: set[int] = set()
    for text_node in root.find_all(string=DEADLINE_LABEL_RE):
        el = text_node.parent
        if not isinstance(el, Tag) or _skip_anchor(el):
            continue
        block = el
        while isinstance(block.parent, Tag) and block.parent is not root and _label_count(block.parent) <= 1:
            block = block.parent
        if id(block) in seen_blocks:
            continue
        seen_blocks.add(id(block))
        text = clean(block.get_text(" "))
        m = DEADLINE_LABEL_RE.search(text)
        deadline = find_datetime(text[m.end(): m.end() + 120]) if m else None
        title = _assignment_title(block, text)
        submitted: bool | None = None
        if NOT_SUBMITTED_RE.search(text):
            submitted = False
        elif SUBMITTED_RE.search(text):
            submitted = True
        ass_id = None
        for a in block.find_all("a", href=True):
            mm = re.search(r"ass_id=(\d+)", a["href"])
            if mm:
                ass_id = mm.group(1)
                break
        results.append(Assignment(title, iso(deadline), submitted, ass_id, text[:500]))
    return results


def _assignment_title(block: Tag, text: str) -> str:
    candidates = block.find_all(
        lambda t: isinstance(t, Tag) and (
            t.name in ("h1", "h2", "h3", "h4", "h5")
            or re.search(r"title|head|header|heading", " ".join(t.get("class", []) or []), re.I)
        )
    )
    for c in candidates:
        t = clean(c.get_text(" "))
        if t and not DEADLINE_LABEL_RE.search(t) and len(t) < 200:
            return t
    # Fallback: Text vor dem ersten Label
    m = DEADLINE_LABEL_RE.search(text)
    head = clean(text[: m.start()] if m else text)
    return head[:120] or "Aufgabe"


def forum_signature(props_text: str) -> tuple[str, int | None]:
    """Fingerabdruck für Foren: Datum des letzten Beitrags, sonst Beitragszahl."""
    m = re.search(r"(?:Letzter Beitrag|Last Posting|Neuester Beitrag)\s*:?\s*(.{0,60})", props_text, re.I)
    unread = None
    mu = re.search(r"(\d+)\s*(?:ungelesen|unread)|(?:Ungelesen\w*|Unread)\s*:?\s*(\d+)", props_text, re.I)
    if mu:
        unread = int(mu.group(1) or mu.group(2))
    if m:
        dt = find_datetime(m.group(1))
        return (iso(dt) if dt else clean(m.group(1))), unread
    mp = re.search(r"(?:Beiträge|Postings|Threads|Themen)\s*:?\s*(\d+)", props_text, re.I)
    if mp:
        return f"posts:{mp.group(1)}", unread
    return props_text, unread


class IliasClient:
    def __init__(self, cfg: IliasConfig, data_path: Path, debug: bool = False):
        self.cfg = cfg
        self.base = cfg.base_url.rstrip("/")
        self.data_path = data_path
        self.debug = debug
        self.session: requests.Session = new_session()
        self.cookie_file = data_path / "ilias_cookies.pkl"
        self._load_cookies()
        self.requests_made = 0
        self.max_requests = 600
        self._debug_n = 0

    # ---------- Session ----------
    def _load_cookies(self) -> None:
        if self.cookie_file.exists():
            try:
                with open(self.cookie_file, "rb") as fh:
                    self.session.cookies.update(pickle.load(fh))
            except Exception:  # noqa: BLE001 - kaputte Datei einfach ignorieren
                pass

    def _save_cookies(self) -> None:
        with open(self.cookie_file, "wb") as fh:
            pickle.dump(self.session.cookies, fh)
        self.cookie_file.chmod(0o600)

    def _dump(self, name: str, html: str) -> None:
        if not self.debug:
            return
        d = self.data_path / "debug" / "ilias"
        d.mkdir(parents=True, exist_ok=True)
        self._debug_n += 1
        (d / f"{self._debug_n:03d}_{slug(name)[:60]}.html").write_text(html, encoding="utf-8")

    def url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def fetch(self, url: str, name: str = "", retry_login: bool = True) -> requests.Response:
        if self.requests_made >= self.max_requests:
            raise SourceError("Zu viele Anfragen in einem Durchlauf – Abbruch zum Schutz des Servers.")
        self.requests_made += 1
        time.sleep(0.25)
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        if retry_login and self._is_login_page(resp.text):
            log.info("ILIAS-Session abgelaufen, melde neu an")
            self.login(force=True)
            return self.fetch(url, name, retry_login=False)
        self._dump(name or url, resp.text)
        return resp

    @staticmethod
    def _is_logged_in(html: str) -> bool:
        return bool(re.search(r"cmd=logout|logout\.php|ilias\.php\?[^\"']*logout", html, re.I))

    def _is_login_page(self, html: str) -> bool:
        return (not self._is_logged_in(html)) and bool(re.search(r'type=["\']?password', html, re.I))

    # ---------- Login ----------
    def login(self, force: bool = False) -> None:
        if not self.cfg.username or not self.cfg.password:
            raise LoginError("ILIAS-Benutzername/Passwort fehlen in config.toml")
        if not force:
            try:
                r = self.session.get(self.url("ilias.php?baseClass=ilDashboardGUI&cmd=show"), timeout=30)
                if self._is_logged_in(r.text):
                    return
            except requests.RequestException:
                pass
        self.session.cookies.clear()
        method = self.cfg.login_method
        errors = []
        if method in ("auto", "local"):
            try:
                self._login_local()
                self._save_cookies()
                return
            except LoginError as exc:
                errors.append(f"ILIAS-Formular: {exc}")
        if method in ("auto", "shibboleth"):
            try:
                self._login_shibboleth()
                self._save_cookies()
                return
            except LoginError as exc:
                errors.append(f"Shibboleth: {exc}")
        raise LoginError("; ".join(errors) or "Login fehlgeschlagen")

    def _login_page(self) -> requests.Response:
        return self.session.get(
            self.url(f"login.php?client_id={self.cfg.client_id}&cmd=force_login&lang=de"), timeout=30
        )

    def _login_local(self) -> None:
        r = self._login_page()
        self._dump("login_page", r.text)
        form = find_login_form(r.text)
        if form is None:
            raise LoginError("kein Login-Formular gefunden")
        action, data = fill_login_form(form, r.url, self.cfg.username, self.cfg.password)
        r2 = self.session.post(action, data=data, timeout=30)
        r2 = follow_autosubmit(self.session, r2)
        self._dump("login_result", r2.text)
        if "duosecurity" in r2.url:
            raise LoginError("ILIAS verlangt eine Duo-Bestätigung – das wird für ILIAS nicht unterstützt")
        if not self._is_logged_in(r2.text):
            msg = self._login_error_message(r2.text)
            raise LoginError(msg or "Anmeldung nicht erfolgreich (Zugangsdaten prüfen)")

    def _login_shibboleth(self) -> None:
        r = self._login_page()
        soup = BeautifulSoup(r.text, "html.parser")
        link = None
        for a in soup.find_all("a", href=True):
            if re.search(r"shib", a["href"], re.I) or re.search(r"shibboleth|uni-account|single sign", a.get_text(" "), re.I):
                link = urljoin(r.url, a["href"])
                break
        if not link:
            link = self.url("shib_login.php")
        r = self.session.get(link, timeout=30)
        r = follow_autosubmit(self.session, r)
        form = find_login_form(r.text)
        if form is None:
            raise LoginError("keine Shibboleth-Anmeldeseite gefunden")
        action, data = fill_login_form(form, r.url, self.cfg.username, self.cfg.password)
        r2 = self.session.post(action, data=data, timeout=30)
        r2 = follow_autosubmit(self.session, r2)
        self._dump("shib_result", r2.text)
        if "duosecurity" in r2.url:
            raise LoginError("ILIAS verlangt eine Duo-Bestätigung")
        if not self._is_logged_in(r2.text):
            r3 = self.session.get(self.url("ilias.php?baseClass=ilDashboardGUI&cmd=show"), timeout=30)
            if not self._is_logged_in(r3.text):
                raise LoginError(self._login_error_message(r2.text) or "Anmeldung nicht erfolgreich")

    @staticmethod
    def _login_error_message(html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        el = soup.select_one(".alert-danger, .ilFailureMessage, .alert-warning, .form-error, .output--error")
        return clean(el.get_text(" "))[:200] if el else ""

    # ---------- Abruf ----------
    def courses(self) -> list[ObjLink]:
        candidates = [
            "ilias.php?baseClass=ilmembershipoverviewgui",
            "ilias.php?baseClass=ilDashboardGUI&cmd=jumpToMemberships",
            "ilias.php?baseClass=ilDashboardGUI&cmd=show",
        ]
        found: dict[str, ObjLink] = {}
        for path in candidates:
            try:
                r = self.fetch(self.url(path), "memberships")
            except requests.HTTPError:
                continue
            for link in membership_links(r.text, r.url):
                found.setdefault(f"{link.type}:{link.ref_id}", link)
            if found:
                break
        excl = [e.lower() for e in self.cfg.exclude_courses]
        return [c for c in found.values() if not any(e in c.title.lower() for e in excl)]

    def crawl(self) -> tuple[list[Item], list[str]]:
        """Alle Kurse durchsuchen. Gibt (Einträge, Fehlermeldungen) zurück."""
        self.login()
        items: list[Item] = []
        errors: list[str] = []
        courses = self.courses()
        if not courses:
            errors.append("Keine Kurse/Gruppen auf dem Dashboard gefunden.")
        visited: set[str] = set()
        for course in courses:
            items.append(Item(
                source="ilias", ext_id=f"{course.type}:{course.ref_id}", kind="course",
                title=course.title, course=course.title, url=course.url, notify=False,
            ))
            try:
                self._crawl_container(course, course.title, 0, items, visited, errors)
            except (requests.RequestException, SourceError) as exc:
                errors.append(f"{course.title}: {exc}")
        self._save_cookies()
        return items, errors

    def _crawl_container(self, obj: ObjLink, course: str, depth: int, items: list[Item],
                         visited: set[str], errors: list[str], path: str = "") -> None:
        key = f"{obj.type}:{obj.ref_id}"
        if key in visited:
            return
        visited.add(key)
        r = self.fetch(self.url(f"goto.php?target={obj.type}_{obj.ref_id}&client_id={self.cfg.client_id}"),
                       f"{obj.type}_{obj.title}")
        for child in container_items(r.text, r.url, own_ref=obj.ref_id):
            where = f"{path} › {child.title}" if path else child.title
            try:
                self._handle_child(child, course, depth, items, visited, errors, path)
            except (requests.RequestException, SourceError) as exc:
                errors.append(f"{course} › {where}: {exc}")

    def _handle_child(self, child: ObjLink, course: str, depth: int, items: list[Item],
                      visited: set[str], errors: list[str], path: str) -> None:
        typ = child.type
        props = child.props_text
        detail = " · ".join(p for p in [path, props] if p)
        base = dict(source="ilias", course=course, url=child.url)

        if typ in ("fold", "grp", "sess", "itgr", "lso"):
            if typ == "sess":
                dt = find_datetime(child.title + " " + props, default_time=dtime(0, 0))
                items.append(Item(ext_id=f"sess:{child.ref_id}", kind="event", title=child.title,
                                  due_at=iso(dt), detail=detail, notify=False, **base))
            elif typ == "fold":
                items.append(Item(ext_id=f"fold:{child.ref_id}", kind="fold", title=child.title,
                                  detail=detail, notify=False, **base))
            if depth < self.cfg.max_depth:
                sub_path = f"{path} › {child.title}" if path else child.title
                self._crawl_container(child, course, depth + 1, items, visited, errors, sub_path)
            return

        if typ == "exc":
            items.append(Item(ext_id=f"exc:{child.ref_id}", kind="exercise", title=child.title,
                              detail=detail, sig="", **base))
            if self.cfg.open_exercises and f"exc:{child.ref_id}" not in visited:
                visited.add(f"exc:{child.ref_id}")
                r = self.fetch(
                    self.url(f"goto.php?target=exc_{child.ref_id}&client_id={self.cfg.client_id}"),
                    f"exc_{child.title}",
                )
                for ass in exercise_assignments(r.text):
                    ass_key = ass.ass_id or slug(ass.title)[:60]
                    status = {True: "abgegeben", False: "nicht abgegeben", None: ""}[ass.submitted]
                    items.append(Item(
                        ext_id=f"exc:{child.ref_id}:ass:{ass_key}", kind="deadline",
                        title=f"{ass.title}", due_at=ass.deadline,
                        detail=" · ".join(x for x in [child.title, status] if x),
                        sig=ass.deadline or "",
                        data={"exercise": child.title, "submitted": ass.submitted},
                        **base,
                    ))
            return

        if typ == "tst":
            due = None
            m = re.search(r"(?:bis|until|Ende|Zugang|Verfügbarkeit|access)[^0-9a-zA-Z]{0,5}(.{0,80})", props, re.I)
            if m:
                due = find_datetime(m.group(1))
            items.append(Item(ext_id=f"tst:{child.ref_id}", kind="test", title=child.title,
                              due_at=iso(due), detail=detail, sig=iso(due) or "", **base))
            return

        if typ == "frm":
            sig, unread = forum_signature(props)
            items.append(Item(ext_id=f"frm:{child.ref_id}", kind="forum", title=child.title,
                              detail=detail, sig=sig, data={"unread": unread}, **base))
            return

        if typ == "file":
            items.append(Item(ext_id=f"file:{child.ref_id}", kind="file", title=child.title,
                              detail=detail, sig=_file_sig(props), **base))
            return

        items.append(Item(ext_id=f"{typ}:{child.ref_id}", kind=typ, title=child.title,
                          detail=" · ".join(x for x in [TYPE_NAMES.get(typ, typ), detail] if x),
                          **base))

    # ---------- Neuigkeiten-Feed (optional) ----------
    def news_feed(self) -> list[Item]:
        if not self.cfg.news_feed_url:
            return []
        from .feeds import parse_rss

        auth = (self.cfg.username, self.cfg.news_feed_password) if self.cfg.news_feed_password else None
        r = requests.get(self.cfg.news_feed_url, auth=auth, timeout=30)
        r.raise_for_status()
        out = []
        for e in parse_rss(r.text):
            course, _, title = e["title"].partition(": ")
            if not title:
                course, title = "", e["title"]
            out.append(Item(source="ilias-news", ext_id=e["id"], kind="news", title=title,
                            course=course, url=e["link"], detail=e["summary"][:300],
                            data={"published": e["published"]}))
        return out


def _file_sig(props: str) -> str:
    """Fingerabdruck einer Datei: Version + Änderungsdatum (Größe kann schwanken)."""
    parts = []
    m = re.search(r"Version\s*:?\s*(\d+)", props, re.I)
    if m:
        parts.append(f"v{m.group(1)}")
    m = re.search(r"(?:Hochgeladen|Geändert|Zuletzt geändert|Uploaded|Last update|Changed)\s*:?\s*(.{0,40})", props, re.I)
    if m:
        dt = find_datetime(m.group(1))
        parts.append(iso(dt) if dt else clean(m.group(1)))
    return "|".join(parts)
