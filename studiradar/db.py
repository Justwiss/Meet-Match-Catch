"""SQLite-Speicher für Einträge, eigene Aufgaben, Push-Abos und Status."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .dates import iso, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,          -- ilias | klips | manual | ical:<name>
    ext_id TEXT NOT NULL,          -- stabile ID innerhalb der Quelle
    kind TEXT NOT NULL,            -- file, exercise, deadline, forum, news, grade, exam, event, task, ...
    title TEXT NOT NULL,
    course TEXT DEFAULT '',
    url TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    due_at TEXT,                   -- ISO-Zeit (Frist / Termin)
    sig TEXT DEFAULT '',           -- Fingerabdruck zur Änderungserkennung
    first_seen TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_sync TEXT,
    seen INTEGER DEFAULT 0,        -- in der App gelesen
    done INTEGER DEFAULT 0,        -- erledigt (Fristen/Aufgaben)
    hidden INTEGER DEFAULT 0,
    gone INTEGER DEFAULT 0,        -- in der Quelle nicht mehr vorhanden
    data TEXT DEFAULT '{}',
    UNIQUE(source, ext_id)
);
CREATE INDEX IF NOT EXISTS idx_items_due ON items(due_at);
CREATE INDEX IF NOT EXISTS idx_items_updated ON items(updated_at);

CREATE TABLE IF NOT EXISTS reminders_sent (
    item_id INTEGER NOT NULL,
    hours INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (item_id, hours)
);

CREATE TABLE IF NOT EXISTS push_subs (
    endpoint TEXT PRIMARY KEY,
    sub_json TEXT NOT NULL,
    created TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started TEXT NOT NULL,
    finished TEXT,
    status TEXT,
    message TEXT,
    new_count INTEGER DEFAULT 0
);
"""


@dataclass
class Item:
    """Ein Eintrag, wie ihn eine Quelle liefert."""

    source: str
    ext_id: str
    kind: str
    title: str
    course: str = ""
    url: str = ""
    detail: str = ""
    due_at: str | None = None
    sig: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    # notify=False: in der Liste anzeigen, aber keine Push-Nachricht ("neu")
    notify: bool = True


@dataclass
class UpsertResult:
    new: list[dict] = field(default_factory=list)
    changed: list[dict] = field(default_factory=list)


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- Hilfen ----------
    def q(self, sql: str, params: Iterable = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def x(self, sql: str, params: Iterable = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur.lastrowid or cur.rowcount

    def get(self, key: str, default: str | None = None) -> str | None:
        rows = self.q("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set(self, key: str, value: str | None) -> None:
        self.x(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---------- Einträge aus Quellen ----------
    def upsert_items(self, source: str, items: list[Item], baseline: bool = False,
                     mark_gone: bool = True) -> UpsertResult:
        """Einträge einer Quelle speichern und Neues/Geändertes zurückgeben.

        baseline=True: erster Abruf – alles als gelesen speichern, nichts melden.
        """
        result = UpsertResult()
        ts = iso(now())
        seen_ids: set[str] = set()
        with self._lock:
            for it in items:
                if it.ext_id in seen_ids:
                    continue
                seen_ids.add(it.ext_id)
                row = self.conn.execute(
                    "SELECT * FROM items WHERE source=? AND ext_id=?", (source, it.ext_id)
                ).fetchone()
                payload = json.dumps(it.data, ensure_ascii=False)
                if row is None:
                    quiet = baseline or not it.notify
                    cur = self.conn.execute(
                        "INSERT INTO items(source, ext_id, kind, title, course, url, detail, due_at,"
                        " sig, first_seen, updated_at, last_seen_sync, seen, data)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (source, it.ext_id, it.kind, it.title, it.course, it.url, it.detail,
                         it.due_at, it.sig, ts, ts, ts, 1 if quiet else 0, payload),
                    )
                    if not quiet:
                        result.new.append(self._row(cur.lastrowid))
                else:
                    changed = bool(it.sig) and it.sig != row["sig"]
                    self.conn.execute(
                        "UPDATE items SET kind=?, title=?, course=?, url=?, detail=?, due_at=?,"
                        " sig=?, last_seen_sync=?, gone=0, data=?"
                        + (", updated_at=?, seen=0" if changed and not baseline and it.notify else "")
                        + " WHERE id=?",
                        (it.kind, it.title, it.course, it.url, it.detail, it.due_at, it.sig, ts,
                         payload)
                        + ((ts,) if changed and not baseline and it.notify else ())
                        + (row["id"],),
                    )
                    if changed and not baseline and it.notify:
                        result.changed.append(self._row(row["id"]))
            if mark_gone:
                self.conn.execute(
                    "UPDATE items SET gone=1 WHERE source=? AND (last_seen_sync IS NULL OR last_seen_sync<>?)",
                    (source, ts),
                )
            self.conn.commit()
        return result

    def _row(self, item_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return dict(row) if row else {}

    def item(self, item_id: int) -> dict | None:
        rows = self.q("SELECT * FROM items WHERE id=?", (item_id,))
        return rows[0] if rows else None

    # ---------- Eigene Aufgaben ----------
    def add_task(self, title: str, due_at: str | None, course: str = "", detail: str = "") -> dict:
        ts = iso(now())
        ext = f"task-{uuid.uuid4().hex}"
        item_id = self.x(
            "INSERT INTO items(source, ext_id, kind, title, course, detail, due_at, first_seen,"
            " updated_at, last_seen_sync, seen) VALUES('manual', ?, 'task', ?, ?, ?, ?, ?, ?, ?, 1)",
            (ext, title, course, detail, due_at, ts, ts, ts),
        )
        return self.item(item_id)

    def update_item(self, item_id: int, **fields) -> dict | None:
        allowed = {"title", "course", "detail", "due_at", "done", "seen", "hidden"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if sets:
            cols = ", ".join(f"{k}=?" for k in sets)
            self.x(f"UPDATE items SET {cols} WHERE id=?", (*sets.values(), item_id))
        return self.item(item_id)

    def delete_task(self, item_id: int) -> None:
        self.x("DELETE FROM items WHERE id=? AND source='manual'", (item_id,))

    # ---------- Abfragen für die App ----------
    def feed(self, limit: int = 100) -> list[dict]:
        return self.q(
            "SELECT * FROM items WHERE hidden=0 AND source<>'manual' AND kind NOT IN ('event', 'course', 'fold')"
            " ORDER BY updated_at DESC, id DESC LIMIT ?",
            (limit,),
        )

    def deadlines(self, include_done: bool = False, past_days: int = 3) -> list[dict]:
        from datetime import timedelta

        since = iso(now() - timedelta(days=past_days))
        sql = (
            "SELECT * FROM items WHERE hidden=0 AND due_at IS NOT NULL AND due_at>=?"
            " AND kind NOT IN ('event') AND gone=0"
        )
        if not include_done:
            sql += " AND done=0"
        sql += " ORDER BY due_at ASC"
        return self.q(sql, (since,))

    def tasks_without_due(self) -> list[dict]:
        return self.q(
            "SELECT * FROM items WHERE source='manual' AND due_at IS NULL AND hidden=0"
            " ORDER BY done ASC, first_seen DESC"
        )

    def events(self, days: int = 7) -> list[dict]:
        from datetime import timedelta

        start = iso(now() - timedelta(hours=2))
        end = iso(now() + timedelta(days=days))
        return self.q(
            "SELECT * FROM items WHERE kind='event' AND hidden=0 AND gone=0 AND due_at>=? AND due_at<=?"
            " ORDER BY due_at ASC",
            (start, end),
        )

    def unread_count(self) -> int:
        return self.q(
            "SELECT COUNT(*) AS n FROM items WHERE seen=0 AND hidden=0 AND source<>'manual'"
            " AND kind NOT IN ('event', 'course', 'fold')"
        )[0]["n"]

    def mark_all_seen(self) -> None:
        self.x("UPDATE items SET seen=1 WHERE seen=0")

    # ---------- Erinnerungen ----------
    def reminder_sent(self, item_id: int, hours: int) -> bool:
        return bool(self.q(
            "SELECT 1 FROM reminders_sent WHERE item_id=? AND hours=?", (item_id, hours)
        ))

    def mark_reminder(self, item_id: int, hours: int) -> None:
        self.x(
            "INSERT OR IGNORE INTO reminders_sent(item_id, hours, sent_at) VALUES(?,?,?)",
            (item_id, hours, iso(now())),
        )

    # ---------- Push ----------
    def add_sub(self, sub: dict) -> None:
        self.x(
            "INSERT INTO push_subs(endpoint, sub_json, created) VALUES(?,?,?)"
            " ON CONFLICT(endpoint) DO UPDATE SET sub_json=excluded.sub_json",
            (sub["endpoint"], json.dumps(sub), iso(now())),
        )

    def remove_sub(self, endpoint: str) -> None:
        self.x("DELETE FROM push_subs WHERE endpoint=?", (endpoint,))

    def subs(self) -> list[dict]:
        return [json.loads(r["sub_json"]) for r in self.q("SELECT sub_json FROM push_subs")]

    # ---------- Sync-Protokoll ----------
    def log_start(self, source: str) -> int:
        return self.x(
            "INSERT INTO sync_log(source, started, status) VALUES(?,?, 'running')",
            (source, iso(now())),
        )

    def log_end(self, log_id: int, status: str, message: str = "", new_count: int = 0) -> None:
        self.x(
            "UPDATE sync_log SET finished=?, status=?, message=?, new_count=? WHERE id=?",
            (iso(now()), status, message[:2000], new_count, log_id),
        )
        # Protokoll klein halten
        self.x("DELETE FROM sync_log WHERE id NOT IN (SELECT id FROM sync_log ORDER BY id DESC LIMIT 300)")

    def last_syncs(self) -> list[dict]:
        return self.q(
            "SELECT s.* FROM sync_log s JOIN (SELECT source, MAX(id) AS mid FROM sync_log GROUP BY source) m"
            " ON s.id = m.mid ORDER BY s.source"
        )
