"""Settings: add-on options from Home Assistant, or a local JSON file for testing on a PC.

Inside the add-on:  /data/options.json (written by the Supervisor), /data for state,
/config for the user-editable sources.yaml, and the Supervisor proxy for the HA API.
Locally: set HOME_NEWS_OPTIONS, HOME_NEWS_DATA, HA_URL and HA_TOKEN.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

APP_DIR = Path(__file__).resolve().parent


@dataclass
class Settings:
    anthropic_api_key: str = ""
    sorter_model: str = "claude-haiku-4-5"
    editor_model: str = "claude-sonnet-5-5"
    refresh_minutes: int = 30
    active_start: str = "06:00"
    active_end: str = "23:59"
    timezone: str = "America/Chicago"
    steam_app_ids: list[int] = field(default_factory=list)
    notify_target: str = ""
    mode_entity: str = ""
    editor_trigger_state: str = "Idle"

    data_dir: Path = Path("/data")
    config_dir: Path = Path("/config")
    ha_url: str = "http://supervisor/core"
    ha_token: str = ""

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def has_ai(self) -> bool:
        return bool(self.anthropic_api_key.strip())


def load_settings() -> Settings:
    options_path = Path(os.environ.get("HOME_NEWS_OPTIONS", "/data/options.json"))
    raw = json.loads(options_path.read_text(encoding="utf-8")) if options_path.exists() else {}
    known = {k: v for k, v in raw.items() if k in Settings.__dataclass_fields__}
    s = Settings(**known)
    s.data_dir = Path(os.environ.get("HOME_NEWS_DATA", "/data"))
    s.config_dir = Path(os.environ.get("HOME_NEWS_CONFIG", "/config"))
    s.ha_url = os.environ.get("HA_URL", "http://supervisor/core").rstrip("/")
    s.ha_token = os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN", "")
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s


def load_sources(settings: Settings) -> dict:
    """User copy in /config/sources.yaml wins; the bundled default seeds it on first run."""
    bundled = APP_DIR / "sources.yaml"
    if not bundled.exists():  # running from the repo checkout
        bundled = APP_DIR.parent / "sources.yaml"
    user = settings.config_dir / "sources.yaml"
    try:
        if settings.config_dir.exists() and not user.exists():
            shutil.copy(bundled, user)
    except OSError:
        pass
    path = user if user.exists() else bundled
    return yaml.safe_load(path.read_text(encoding="utf-8"))
