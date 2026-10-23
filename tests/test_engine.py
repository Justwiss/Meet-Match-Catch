import json
from datetime import datetime, timedelta

import pytest

from studiradar.config import Config
from studiradar.dates import TZ, iso
from studiradar.db import Item
from studiradar.engine import Engine
from studiradar.sources.feeds import parse_ical
from studiradar.sources.klips import rows_to_items


@pytest.fixture
def engine(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    cfg.app.secret_key = "x"
    e = Engine(cfg)
    sent = []
    e.pusher.send = lambda title, body, url="/", tag=None, urgent=False: sent.append((title, body, url)) or 1
    e.sent = sent
    return e


def test_baseline_then_new_and_changed(engine):
    items = [Item("ilias", "file:1", "file", "Folien 1", course="BWL", sig="v1")]
    engine._store("ilias", items, "ILIAS")
    assert engine.sent == []  # erster Abruf: nur merken
    assert engine.db.unread_count() == 0

    items = [
        Item("ilias", "file:1", "file", "Folien 1", course="BWL", sig="v2"),
        Item("ilias", "file:2", "file", "Folien 2", course="BWL", sig="v1"),
    ]
    n = engine._store("ilias", items, "ILIAS")
    assert n == 2
    assert engine.sent[-1][0] == "2 Neuigkeiten aus ILIAS"
    assert engine.db.unread_count() == 2

    # Gleicher Stand → nichts Neues
    engine._store("ilias", items, "ILIAS")
    assert len(engine.sent) == 1


def test_single_item_message(engine):
    engine._store("ilias", [], "ILIAS")
    engine._store("ilias", [Item("ilias", "exc:1:ass:2", "deadline", "Blatt 2", course="WI",
                                 due_at=iso(datetime.now(TZ) + timedelta(days=2)))], "ILIAS")
    title, body, url = engine.sent[-1]
    assert title == "Neue Frist · WI"
    assert body.startswith("Blatt 2 (bis ")
    assert url.startswith("/#item-")


def test_reminders_once_and_smallest_window(engine):
    db = engine.db
    task = db.add_task("Hausarbeit abgeben", iso(datetime.now(TZ) + timedelta(hours=20)))
    assert engine.check_reminders() == 1
    assert "Hausarbeit" in engine.sent[-1][1]
    # 72h- und 24h-Erinnerung zählen als erledigt, keine doppelte Nachricht
    assert engine.check_reminders() == 0
    assert db.reminder_sent(task["id"], 72) and db.reminder_sent(task["id"], 24)
    assert not db.reminder_sent(task["id"], 3)


def test_reminders_skip_done_and_submitted(engine):
    db = engine.db
    t = db.add_task("Erledigt", iso(datetime.now(TZ) + timedelta(hours=2)))
    db.update_item(t["id"], done=1)
    engine._store("ilias", [Item("ilias", "exc:1:ass:1", "deadline", "Abgegeben",
                                 due_at=iso(datetime.now(TZ) + timedelta(hours=2)),
                                 data={"submitted": True})], "ILIAS")
    assert engine.check_reminders() == 0


def test_digest(engine):
    assert engine.digest() is False
    engine.db.add_task("Lernen", iso(datetime.now(TZ) + timedelta(hours=30)))
    assert engine.digest() is True
    assert "Lernen" in engine.sent[-1][1]


def test_quiet_hours_queue(engine, monkeypatch):
    from studiradar.notify import Pusher

    p = Pusher(engine.db, engine.cfg.data_path, "a@b.de", quiet=("00:00", "23:59"))
    assert p.send("Hallo", "Welt") == 0
    assert json.loads(engine.db.get("pending_push"))[0]["title"] == "Hallo"


def test_vapid_public_key(engine):
    key = engine.pusher.public_key
    assert len(key) == 87  # 65 Bytes base64url ohne Padding


ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//ILIAS//DE
BEGIN:VEVENT
UID:ev1
SUMMARY:Übungsblatt 3: Abgabe
CATEGORIES:Wirtschaftsinformatik
DTSTART;TZID=Europe/Berlin:{d1}T235900
DTEND;TZID=Europe/Berlin:{d1}T235900
END:VEVENT
BEGIN:VEVENT
UID:ev2
SUMMARY:Vorlesung BWL
LOCATION:Hörsaal C
DTSTART;TZID=Europe/Berlin:{d2}T100000
DTEND;TZID=Europe/Berlin:{d2}T113000
RRULE:FREQ=WEEKLY;COUNT=3
END:VEVENT
BEGIN:VEVENT
UID:ev3
SUMMARY:Klausur Statistik
DTSTART;VALUE=DATE:{d3}
END:VEVENT
END:VCALENDAR
"""


def test_parse_ical():
    now = datetime.now(TZ)
    fmt = lambda d: d.strftime("%Y%m%d")
    text = ICS.format(d1=fmt(now + timedelta(days=5)), d2=fmt(now + timedelta(days=1)), d3=fmt(now + timedelta(days=40)))
    items = parse_ical(text, "ical:ilias")
    kinds = sorted((i.kind, i.title) for i in items)
    assert ("deadline", "Übungsblatt 3: Abgabe") in kinds
    assert ("exam", "Klausur Statistik") in kinds
    lectures = [i for i in items if i.title == "Vorlesung BWL"]
    assert len(lectures) == 3 and all(i.kind == "event" and not i.notify for i in lectures)
    assert "Hörsaal C" in lectures[0].detail
    dl = next(i for i in items if i.kind == "deadline")
    assert dl.course == "Wirtschaftsinformatik" and "T23:59" in dl.due_at


def test_klips_rows():
    rows = [
        "Datenbanken 1,7 bestanden 15.08.2026",
        "Abmelden",
        "Statistik II Klausur 12.02.2027 Anmeldung bis 15.01.2027",
        "kurz",
    ]
    grades = rows_to_items("Noten", rows, "https://klips")
    assert [g.kind for g in grades] == ["grade", "grade"]
    exams = rows_to_items("Prüfungen", rows, "https://klips")
    reg = [e for e in exams if e.kind == "deadline"]
    assert len(reg) == 1 and reg[0].due_at.startswith("2027-01-15")
