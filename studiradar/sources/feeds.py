"""Kalender-Abos (iCal) und RSS-Feeds einlesen – ganz ohne Login."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import requests
from dateutil.rrule import rrulestr
from icalendar import Calendar

from ..dates import TZ, iso, now
from ..db import Item
from .common import clean, new_session

DEADLINE_RE = re.compile(
    r"abgabe|deadline|frist|submission|einreich|fällig|due|anmeldeschluss|anmeldefrist|"
    r"abmeldefrist|rückmeldung",
    re.I,
)
EXAM_RE = re.compile(r"prüfung|klausur|exam|test\b|testat|kolloquium", re.I)


def fetch_text(url: str, auth: tuple[str, str] | None = None) -> str:
    url = url.replace("webcal://", "https://", 1)
    r = new_session().get(url, auth=auth, timeout=30)
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    return r.text


def _to_dt(value) -> tuple[datetime, bool]:
    """iCal-Wert → (Zeitpunkt in Berlin, ganztägig?)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=TZ)
        return value.astimezone(TZ), False
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, 0, 0, tzinfo=TZ), True
    raise ValueError(value)


def parse_ical(text: str, source: str, horizon_days: int = 120, past_days: int = 14) -> list[Item]:
    cal = Calendar.from_ical(text)
    start_window = now() - timedelta(days=past_days)
    end_window = now() + timedelta(days=horizon_days)
    out: list[Item] = []
    for ev in cal.walk("VEVENT"):
        summary = clean(str(ev.get("SUMMARY", "")))
        if not summary:
            continue
        desc = clean(str(ev.get("DESCRIPTION", "")))
        location = clean(str(ev.get("LOCATION", "")))
        categories = ev.get("CATEGORIES")
        course = ""
        if categories is not None:
            try:
                cats = categories if isinstance(categories, list) else [categories]
                course = ", ".join(clean(str(c)) for cat in cats for c in getattr(cat, "cats", [cat]))
            except Exception:  # noqa: BLE001
                course = ""
        uid = str(ev.get("UID", "")) or hashlib.sha1(f"{summary}{ev.get('DTSTART')}".encode()).hexdigest()
        try:
            start, all_day = _to_dt(ev.decoded("DTSTART"))
        except Exception:  # noqa: BLE001
            continue
        end = None
        if ev.get("DTEND") is not None:
            try:
                end, _ = _to_dt(ev.decoded("DTEND"))
            except Exception:  # noqa: BLE001
                end = None

        starts = [start]
        if ev.get("RRULE"):
            try:
                rule = rrulestr(ev["RRULE"].to_ical().decode(), dtstart=start)
                starts = list(rule.between(start_window, end_window, inc=True))
                exdates = set()
                for ex in (ev.get("EXDATE") if isinstance(ev.get("EXDATE"), list) else [ev.get("EXDATE")]):
                    if ex is None:
                        continue
                    for d in ex.dts:
                        exdates.add(_to_dt(d.dt)[0])
                starts = [s for s in starts if s not in exdates]
            except Exception:  # noqa: BLE001
                starts = [start]

        text = f"{summary} {desc}"
        if DEADLINE_RE.search(text):
            kind = "deadline"
        elif EXAM_RE.search(summary):
            kind = "exam"
        else:
            kind = "event"
        for s in starts:
            if not (start_window <= s <= end_window):
                continue
            due = s
            # Fristen: Ende des Termins ist meist der eigentliche Abgabezeitpunkt
            if kind == "deadline" and end is not None and not all_day and len(starts) == 1:
                due = end
            if all_day and kind == "deadline":
                due = s.replace(hour=23, minute=59)
            detail_parts = []
            if not all_day:
                if end is not None and kind == "event":
                    duration = end - start
                    detail_parts.append(f"bis {(s + duration).strftime('%H:%M')}")
            else:
                detail_parts.append("ganztägig")
            if location:
                detail_parts.append(location)
            if desc and kind != "event":
                detail_parts.append(desc[:200])
            ext = uid if len(starts) == 1 else f"{uid}@{s.date().isoformat()}"
            out.append(Item(
                source=source, ext_id=ext, kind=kind, title=summary, course=course,
                detail=" · ".join(detail_parts), due_at=iso(due), sig=iso(due) or "",
                url=str(ev.get("URL", "")),
                # Normale Termine nicht als "neu" melden, Fristen/Prüfungen schon
                notify=kind != "event",
            ))
    return out


def parse_rss(text: str) -> list[dict]:
    root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    entries = []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for it in root.iter("item"):
        title = clean(it.findtext("title"))
        link = clean(it.findtext("link"))
        guid = clean(it.findtext("guid")) or link or title
        desc = clean(re.sub(r"<[^>]+>", " ", it.findtext("description") or ""))
        pub = it.findtext("pubDate")
        published = None
        if pub:
            try:
                published = iso(parsedate_to_datetime(pub))
            except (TypeError, ValueError):
                published = None
        entries.append({"id": guid, "title": title, "link": link, "summary": desc, "published": published})
    for it in root.iter("{http://www.w3.org/2005/Atom}entry"):
        title = clean(it.findtext("atom:title", namespaces=ns))
        link_el = it.find("atom:link", ns)
        link = link_el.get("href", "") if link_el is not None else ""
        entries.append({
            "id": clean(it.findtext("atom:id", namespaces=ns)) or link,
            "title": title, "link": link,
            "summary": clean(re.sub(r"<[^>]+>", " ", it.findtext("atom:summary", namespaces=ns) or "")),
            "published": clean(it.findtext("atom:updated", namespaces=ns)) or None,
        })
    return entries


def fetch_ical(url: str, source: str) -> list[Item]:
    return parse_ical(fetch_text(url), source)


__all__ = ["fetch_ical", "parse_ical", "parse_rss", "requests"]
