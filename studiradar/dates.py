"""Datumsangaben aus ILIAS/KLIPS (deutsch und englisch) erkennen."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")

MONTHS = {
    "jan": 1, "januar": 1, "january": 1, "jän": 1, "jänner": 1,
    "feb": 2, "februar": 2, "february": 2,
    "mär": 3, "mar": 3, "märz": 3, "maerz": 3, "march": 3, "mrz": 3,
    "apr": 4, "april": 4,
    "mai": 5, "may": 5,
    "jun": 6, "juni": 6, "june": 6,
    "jul": 7, "juli": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "okt": 10, "oct": 10, "oktober": 10, "october": 10,
    "nov": 11, "november": 11,
    "dez": 12, "dec": 12, "dezember": 12, "december": 12,
}

RELATIVE = {
    "heute": 0, "today": 0,
    "morgen": 1, "tomorrow": 1,
    "gestern": -1, "yesterday": -1,
    "übermorgen": 2,
}

_TIME = r"(?:,?\s*(?:um\s*)?(?P<h>\d{1,2})[:.](?P<mi>\d{2})(?:\s*Uhr)?)?"
_MONTH_NAMES = "|".join(sorted(MONTHS, key=len, reverse=True))

PATTERNS = [
    # 23. Okt 2026, 14:00  |  23 October 2026 14:00
    re.compile(
        rf"\b(?P<d>\d{{1,2}})\.?\s*(?P<mon>{_MONTH_NAMES})\.?\s+(?P<y>\d{{4}})" + _TIME,
        re.IGNORECASE,
    ),
    # Oct 23, 2026 14:00 (englische Oberfläche)
    re.compile(
        rf"\b(?P<mon>{_MONTH_NAMES})\.?\s+(?P<d>\d{{1,2}}),?\s+(?P<y>\d{{4}})" + _TIME,
        re.IGNORECASE,
    ),
    # 30.10.2026 23:59 | 30.10.26
    re.compile(r"(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4}|\d{2})(?!\d)" + _TIME),
    # 2026-10-30 23:59 | 2026-10-30T23:59
    re.compile(r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})(?:[T ](?P<h>\d{2}):(?P<mi>\d{2}))?"),
    # Heute, 14:00 | Morgen 23:55
    re.compile(
        r"\b(?P<rel>übermorgen|heute|morgen|gestern|today|tomorrow|yesterday)\b" + _TIME,
        re.IGNORECASE,
    ),
]


def now() -> datetime:
    return datetime.now(TZ)


def find_datetime(
    text: str, ref: datetime | None = None, default_time: time = time(23, 59)
) -> datetime | None:
    """Erstes Datum im Text finden. Ohne Uhrzeit wird `default_time` genommen."""
    if not text:
        return None
    ref = ref or now()
    best: tuple[int, datetime] | None = None
    for pat in PATTERNS:
        for m in pat.finditer(text):
            dt = _build(m, ref, default_time)
            if dt is not None and (best is None or m.start() < best[0]):
                best = (m.start(), dt)
            break  # pro Muster nur der erste Treffer
    return best[1] if best else None


def _build(m: re.Match, ref: datetime, default_time: time) -> datetime | None:
    g = m.groupdict()
    try:
        if g.get("rel"):
            day = (ref + timedelta(days=RELATIVE[g["rel"].lower()])).date()
        else:
            year = int(g["y"])
            if year < 100:
                year += 2000
            if g.get("mon"):
                month = MONTHS[g["mon"].lower().rstrip(".")]
            else:
                month = int(g["m"])
            day = datetime(year, month, int(g["d"])).date()
        if g.get("h") is not None and g.get("mi") is not None:
            t = time(int(g["h"]), int(g["mi"]))
        else:
            t = default_time
        return datetime.combine(day, t, tzinfo=TZ)
    except (ValueError, KeyError):
        return None


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).isoformat(timespec="minutes")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def fmt_relative(dt: datetime, ref: datetime | None = None) -> str:
    """Kurzform für Benachrichtigungen, z.B. 'morgen 23:59' oder 'in 3 Tagen'."""
    ref = ref or now()
    days = (dt.date() - ref.date()).days
    hhmm = dt.strftime("%H:%M")
    if days == 0:
        return f"heute {hhmm}"
    if days == 1:
        return f"morgen {hhmm}"
    if 1 < days < 7:
        return f"in {days} Tagen ({dt.strftime('%d.%m.')} {hhmm})"
    return dt.strftime("%d.%m.%Y %H:%M")
