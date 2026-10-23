"""Konfiguration laden (config.toml) mit sinnvollen Standardwerten."""

from __future__ import annotations

import os
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AppConfig:
    password: str = ""
    secret_key: str = ""
    data_dir: str = "data"
    timezone: str = "Europe/Berlin"
    host: str = "0.0.0.0"
    port: int = 8080
    contact_email: str = "mailto:studiradar@example.com"


@dataclass
class IliasConfig:
    enabled: bool = True
    base_url: str = "https://www.ilias.uni-koeln.de/ilias"
    client_id: str = "uk"
    username: str = ""
    password: str = ""
    # "auto" probiert erst das normale ILIAS-Formular, dann Shibboleth
    login_method: str = "auto"
    interval_minutes: int = 60
    max_depth: int = 3
    # Kalender-Abo-Link aus ILIAS (Dashboard → Kalender → Abonnieren)
    ical_url: str = ""
    # Privater RSS-Feed (Persönliche Einstellungen → Neuigkeiten)
    news_feed_url: str = ""
    news_feed_password: str = ""
    exclude_courses: list[str] = field(default_factory=list)
    open_exercises: bool = True


@dataclass
class KlipsConfig:
    enabled: bool = False
    base_url: str = "https://klips2.uni-koeln.de"
    start_url: str = "https://klips2.uni-koeln.de/co/ee/ui/ca2/app/desktop/#/login"
    username: str = ""
    password: str = ""
    # Kalender-Abo-Link aus KLIPS (Visitenkarte → Kalender → Abonnieren)
    ical_url: str = ""
    # Zeiten für stille Abrufe (klappen nur, solange Duo dich noch "merkt")
    auto_times: list[str] = field(default_factory=lambda: ["08:00", "18:00"])
    chromium_path: str = ""
    headless: bool = True
    duo_timeout_seconds: int = 120
    # Seiten, die nach dem Login gelesen und auf Änderungen geprüft werden
    pages: dict[str, str] = field(
        default_factory=lambda: {
            "Noten": "https://klips2.uni-koeln.de/co/ee/ui/ca2/app/desktop/#/slc.xm.ac/achievements",
            "Prüfungen": "https://klips2.uni-koeln.de/co/ee/ui/ca2/app/desktop/#/slc.xm.exr/exams",
        }
    )


@dataclass
class IcalFeed:
    name: str = ""
    url: str = ""


@dataclass
class NotifyConfig:
    reminders_hours: list[int] = field(default_factory=lambda: [72, 24, 3])
    digest_time: str = "07:30"
    quiet_start: str = "23:00"
    quiet_end: str = "07:00"


@dataclass
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    ilias: IliasConfig = field(default_factory=IliasConfig)
    klips: KlipsConfig = field(default_factory=KlipsConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    ical: list[IcalFeed] = field(default_factory=list)
    path: Path | None = None

    @property
    def data_path(self) -> Path:
        p = Path(self.app.data_dir)
        if not p.is_absolute() and self.path is not None:
            p = self.path.parent / p
        p.mkdir(parents=True, exist_ok=True)
        return p


def _fill(obj, values: dict):
    for key, value in values.items():
        if hasattr(obj, key):
            setattr(obj, key, value)
    return obj


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("STUDIRADAR_CONFIG", "config.toml")).resolve()
    cfg = Config(path=path)
    if path.exists():
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        _fill(cfg.app, raw.get("app", {}))
        _fill(cfg.ilias, raw.get("ilias", {}))
        _fill(cfg.klips, raw.get("klips", {}))
        _fill(cfg.notify, raw.get("notify", {}))
        cfg.ical = [_fill(IcalFeed(), f) for f in raw.get("ical", [])]
    if not cfg.app.secret_key:
        # Schlüssel einmalig erzeugen und im Datenordner merken
        key_file = cfg.data_path / "secret.key"
        if not key_file.exists():
            key_file.write_text(secrets.token_hex(32))
            key_file.chmod(0o600)
        cfg.app.secret_key = key_file.read_text().strip()
    return cfg
