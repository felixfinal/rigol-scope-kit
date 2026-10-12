from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import tomllib

from .errors import ScopeError


def xdg_path(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable)
    if value and Path(value).is_absolute():
        return Path(value)
    return Path.home() / fallback


def default_config() -> Path:
    return Path(os.environ.get("RIGOL_SCOPE_CONFIG", xdg_path("XDG_CONFIG_HOME", ".config") / "rigol-scope-kit/devices.toml"))


@dataclass(frozen=True)
class Device:
    alias: str
    host: str
    port: int
    model: str = "MHO954"
    serial: str = ""
    timeout_s: float = 5.0
    operation_timeout_s: float = 30.0
    max_binary_bytes: int = 8 * 1024 * 1024

    def __post_init__(self):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.alias):
            raise ScopeError("Invalid device alias")
        if not isinstance(self.host, str) or not self.host or re.search(r"[\s/\\]", self.host):
            raise ScopeError("host must be an explicit IP address or hostname")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ScopeError("Explicit SCPI port must be between 1 and 65535")
        if self.model not in {"MHO934", "MHO954", "MHO984"}:
            raise ScopeError("Only explicit MHO900 model profiles are supported")
        if not isinstance(self.serial, str) or any(c.isspace() for c in self.serial):
            raise ScopeError("Invalid expected serial")
        for name in ("timeout_s", "operation_timeout_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 < value <= 120:
                raise ScopeError(f"{name} must be finite, >0 and <=120 seconds")
        if type(self.max_binary_bytes) is not int or not 1 <= self.max_binary_bytes <= 32 * 1024 * 1024:
            raise ScopeError("max_binary_bytes must be between 1 and 33554432")


@dataclass(frozen=True)
class Settings:
    devices: dict[str, Device]
    state_dir: Path
    profiles_dir: Path | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or default_config()
        try:
            raw = tomllib.loads(path.read_text())
            if raw.get("schema_version") != 1:
                raise ScopeError("Expected configuration schema_version = 1")
            devices = {name: Device(alias=name, **values) for name, values in raw["devices"].items()}
            if not devices:
                raise ScopeError("No configured devices")
            state = Path(raw.get("state_dir", xdg_path("XDG_STATE_HOME", ".local/state") / "rigol-scope-kit")).expanduser()
            if not state.is_absolute():
                state = (path.resolve().parent / state).resolve()
            profiles=Path(raw.get("profiles_dir",path.resolve().parent/"profiles")).expanduser()
            if not profiles.is_absolute():profiles=(path.resolve().parent/profiles).resolve()
            return cls(devices, state, profiles)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ScopeError(f"Cannot load configuration {path}: {exc}") from exc

    def device(self, alias: str) -> Device:
        try:
            return self.devices[alias]
        except KeyError as exc:
            raise ScopeError(f"Unknown configured device: {alias}") from exc
