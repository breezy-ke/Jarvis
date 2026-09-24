"""The satellite's settings: a small TOML file you can edit by hand.

Windows: %APPDATA%\\Jarvis\\satellite.toml. Elsewhere: ~/.config/jarvis/satellite.toml.
The device token is never kept here: it lives in Windows Credential Manager.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("jarvis.satellite")


def config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Jarvis"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "jarvis"


def data_dir() -> Path:
    """Where the wake-word models are kept."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "Jarvis" / "satellite"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "jarvis-satellite"


def default_path() -> Path:
    return config_dir() / "satellite.toml"


def _default_name() -> str:
    return (platform.node() or "Windows PC")[:80]


@dataclass
class Settings:
    server: str = "http://localhost:8080"
    name: str = field(default_factory=_default_name)
    wake_threshold: float = 0.5
    # After Jarvis answers, keep listening this long for a follow-up without the wake word.
    follow_up_seconds: float = 6.0
    # ...and this long while an action waits for you to say "confirm" or "cancel".
    confirm_seconds: float = 20.0
    input_device: str = ""
    output_device: str = ""
    mute_hotkey: str = "<ctrl>+<alt>+j"
    chime: bool = True

    def __post_init__(self) -> None:
        parts = urlsplit(self.server.strip())
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"server must look like http://host:port, not {self.server!r}")
        self.server = f"{parts.scheme}://{parts.netloc}"
        self.wake_threshold = min(0.95, max(0.1, float(self.wake_threshold)))
        self.follow_up_seconds = min(30.0, max(0.0, float(self.follow_up_seconds)))
        self.confirm_seconds = min(120.0, max(self.follow_up_seconds, float(self.confirm_seconds)))
        self.name = (self.name.strip() or _default_name())[:80]

    @property
    def socket_url(self) -> str:
        scheme = "wss" if self.server.startswith("https://") else "ws"
        return f"{scheme}://{urlsplit(self.server).netloc}/api/voice/ws"


def load(path: Path | None = None) -> Settings:
    path = path or default_path()
    if not path.exists():
        return Settings()
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    known = {f.name for f in fields(Settings)}
    unknown = sorted(set(raw) - known)
    if unknown:
        log.warning("ignoring unknown settings in %s: %s", path, ", ".join(unknown))
    return Settings(**{k: v for k, v in raw.items() if k in known})


def save(settings: Settings, path: Path | None = None) -> Path:
    path = path or default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Jarvis satellite settings. Restart the app after editing."]
    for f in fields(Settings):
        value = getattr(settings, f.name)
        if isinstance(value, bool):
            lines.append(f"{f.name} = {'true' if value else 'false'}")
        elif isinstance(value, int | float):
            lines.append(f"{f.name} = {value}")
        else:
            lines.append(f"{f.name} = {json.dumps(value)}")  # a valid TOML basic string
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
