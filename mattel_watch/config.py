from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import re
from datetime import datetime, timezone


def launch_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        raise ValueError('launch_at must be an ISO date/time with a UTC offset') from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('launch_at needs an explicit offset, for example 2026-10-20T09:00:00-07:00')
    return parsed.astimezone(timezone.utc)


LEGACY_FIELDS = ("purchase_mode", "quantity", "profile_dir", "headless")


@dataclass
class Config:
    store_url: str = "https://creations.mattel.com/en-ca"
    collections: dict[str, str] = field(default_factory=lambda: {
        "rlc": "hot-wheels-red-line-club", "premium": "hot-wheels-premium"})
    interval_seconds: int = 180
    state_path: str = "data/state.sqlite3"
    max_pages: int = 20
    launch_watches: list[dict] = field(default_factory=list)
    launch_poll_seconds: int = 10
    launch_window_seconds: int = 300
    discovery_poll_seconds: int = 10
    baseline_recheck_seconds: int = 21600

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        # Settings of the removed cart feature are ignored so older configs keep working.
        for legacy in LEGACY_FIELDS:
            raw.pop(legacy, None)
        unknown = set(raw) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError("Unknown config fields: " + ", ".join(sorted(unknown)))
        cfg = cls(**raw)
        cfg.validate()
        return cfg

    def validate(self):
        if self.store_url != "https://creations.mattel.com/en-ca":
            raise ValueError("This build supports the Canadian Mattel storefront only.")
        if type(self.interval_seconds) is not int or self.interval_seconds < 60:
            raise ValueError("interval_seconds must be at least 60")
        if type(self.max_pages) is not int or not 1 <= self.max_pages <= 100:
            raise ValueError("max_pages must be between 1 and 100")
        if not self.collections or set(self.collections) - {"rlc", "premium"}:
            raise ValueError("collections must contain rlc and/or premium")
        for handle in self.collections.values():
            if not isinstance(handle, str) or not re.fullmatch(r"[a-z0-9-]+", handle):
                raise ValueError("Invalid collection handle")
        if type(self.launch_poll_seconds) is not int or self.launch_poll_seconds < 10:
            raise ValueError('launch_poll_seconds must be at least 10')
        if type(self.launch_window_seconds) is not int or not 60 <= self.launch_window_seconds <= 1800:
            raise ValueError('launch_window_seconds must be between 60 and 1800')
        if not isinstance(self.launch_watches, list):
            raise ValueError('launch_watches must be a list')
        if type(self.discovery_poll_seconds) is not int or self.discovery_poll_seconds < 10:
            raise ValueError('discovery_poll_seconds must be at least 10')
        if type(self.baseline_recheck_seconds) is not int or self.baseline_recheck_seconds < 1800:
            raise ValueError('baseline_recheck_seconds must be at least 1800')
        handles = set()
        for watch in self.launch_watches:
            if not isinstance(watch, dict) or set(watch) != {'handle', 'launch_at'}:
                raise ValueError('Each launch watch needs exactly handle and launch_at')
            if not isinstance(watch['handle'], str) or not re.fullmatch(r'[a-z0-9-]+', watch['handle']):
                raise ValueError('Invalid watched product handle')
            if watch['handle'] in handles:
                raise ValueError('Duplicate launch watch')
            handles.add(watch['handle'])
            launch_time(watch['launch_at'])

    @property
    def scope_key(self):
        return json.dumps([self.store_url, self.collections], sort_keys=True)


def load_env(path: str | Path = ".env"):
    """Small literal KEY=VALUE reader. Never executes shell expressions."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key.strip()):
            raise ValueError("Invalid .env line; use literal KEY=value entries")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)
