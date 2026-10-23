"""Gemeinsame Hilfen für die Quellen: HTTP-Session, Login-Formulare, Text."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 StudiRadar/1.0"
)


class SourceError(Exception):
    """Fehler beim Abruf einer Quelle."""


class LoginError(SourceError):
    """Anmeldung fehlgeschlagen."""


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
    })
    return s


def clean(text: str | None) -> str:
    if not text:
        return ""
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower() or "x"


def find_login_form(html: str) -> Tag | None:
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        if form.find("input", attrs={"type": re.compile("password", re.I)}):
            return form
    return None


def fill_login_form(form: Tag, page_url: str, username: str, password: str) -> tuple[str, dict]:
    """Formularfelder übernehmen und Benutzer/Passwort eintragen."""
    data: dict[str, str] = {}
    user_field = None
    pw_field = None
    for inp in form.find_all(["input", "select", "textarea"]):
        name = inp.get("name")
        if not name:
            continue
        typ = (inp.get("type") or "text").lower()
        if typ == "password" and pw_field is None:
            pw_field = name
        elif typ in ("text", "email") and user_field is None:
            user_field = name
        elif typ in ("checkbox", "radio"):
            if inp.has_attr("checked"):
                data[name] = inp.get("value", "on")
        elif typ not in ("submit", "button", "image", "reset"):
            data[name] = inp.get("value", "")
    if pw_field is None:
        raise LoginError("Passwortfeld nicht gefunden")
    if user_field is None:
        # Häufige Namen als Fallback
        for cand in ("username", "j_username", "login", "user"):
            if form.find("input", attrs={"name": cand}):
                user_field = cand
                break
    if user_field is None:
        raise LoginError("Benutzerfeld nicht gefunden")
    data[user_field] = username
    data[pw_field] = password
    # Absende-Knopf mitschicken (ILIAS braucht z.B. cmd[doStandardAuthentication])
    submit = form.find(["input", "button"], attrs={"type": "submit"}) or form.find("button")
    if submit is not None and submit.get("name"):
        data[submit["name"]] = submit.get("value", "")
    action = urljoin(page_url, form.get("action") or page_url)
    return action, data


def follow_autosubmit(session: requests.Session, resp: requests.Response, max_hops: int = 6) -> requests.Response:
    """SAML/Shibboleth-Seiten mit automatisch absendenden Formularen durchlaufen."""
    for _ in range(max_hops):
        soup = BeautifulSoup(resp.text, "html.parser")
        form = None
        for f in soup.find_all("form"):
            names = {i.get("name") for i in f.find_all("input")}
            if names & {"SAMLResponse", "SAMLRequest", "RelayState"}:
                form = f
                break
            visible = [
                i for i in f.find_all("input")
                if (i.get("type") or "text").lower() not in ("hidden", "submit")
            ]
            if not visible and f.find("input", attrs={"type": "hidden"}) and (
                "onload" in str(soup.body.attrs if soup.body else "") or "submit()" in resp.text
            ):
                form = f
                break
        if form is None:
            return resp
        data = {i.get("name"): i.get("value", "") for i in form.find_all("input") if i.get("name")}
        action = urljoin(resp.url, form.get("action") or resp.url)
        method = (form.get("method") or "post").lower()
        if method == "get":
            resp = session.get(action, params=data, timeout=30)
        else:
            resp = session.post(action, data=data, timeout=30)
    return resp
