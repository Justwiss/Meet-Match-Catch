from datetime import datetime, time

from studiradar.dates import TZ, find_datetime, fmt_relative

REF = datetime(2026, 10, 23, 12, 0, tzinfo=TZ)


def test_german_month_names():
    assert find_datetime("Abgabetermin: Freitag, 30. Okt 2026, 23:59", REF) == datetime(2026, 10, 30, 23, 59, tzinfo=TZ)
    assert find_datetime("06. November 2026 12:00 Uhr", REF) == datetime(2026, 11, 6, 12, 0, tzinfo=TZ)
    assert find_datetime("3. März 2027", REF) == datetime(2027, 3, 3, 23, 59, tzinfo=TZ)


def test_english_and_numeric():
    assert find_datetime("Oct 30, 2026 14:00", REF) == datetime(2026, 10, 30, 14, 0, tzinfo=TZ)
    assert find_datetime("bis 15.11.2026, 10:00", REF) == datetime(2026, 11, 15, 10, 0, tzinfo=TZ)
    assert find_datetime("am 15.11.26", REF) == datetime(2026, 11, 15, 23, 59, tzinfo=TZ)
    assert find_datetime("2026-12-01T09:30", REF) == datetime(2026, 12, 1, 9, 30, tzinfo=TZ)


def test_relative_days():
    assert find_datetime("Heute, 18:00", REF) == datetime(2026, 10, 23, 18, 0, tzinfo=TZ)
    assert find_datetime("Morgen, 23:55", REF) == datetime(2026, 10, 24, 23, 55, tzinfo=TZ)
    assert find_datetime("Hochgeladen: Gestern, 10:15", REF) == datetime(2026, 10, 22, 10, 15, tzinfo=TZ)
    assert find_datetime("Today, 18:00", REF) == datetime(2026, 10, 23, 18, 0, tzinfo=TZ)


def test_default_time_and_no_date():
    assert find_datetime("12. Dez 2026", REF, default_time=time(0, 0)) == datetime(2026, 12, 12, 0, 0, tzinfo=TZ)
    assert find_datetime("Version: 2", REF) is None
    assert find_datetime("Morgenkreis", REF) is None
    assert find_datetime("", REF) is None


def test_earliest_match_wins():
    assert find_datetime("vom 01.11.2026 bis 30. Nov 2026", REF) == datetime(2026, 11, 1, 23, 59, tzinfo=TZ)


def test_fmt_relative():
    assert fmt_relative(datetime(2026, 10, 23, 18, 0, tzinfo=TZ), REF) == "heute 18:00"
    assert fmt_relative(datetime(2026, 10, 24, 9, 5, tzinfo=TZ), REF) == "morgen 09:05"
    assert fmt_relative(datetime(2026, 10, 26, 9, 5, tzinfo=TZ), REF).startswith("in 3 Tagen")
