"""Configuration: an INI file for settings plus an env file for Pushover keys.

Check definitions do not live here; they are stored in the database and
managed with the TUI (`watchdog tui`).
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path

SEARCH_PATHS = ("./watchdog.ini", "/etc/watchdog/watchdog.ini")

PUSHOVER_API = "https://api.pushover.net/1/messages.json"


class ConfigError(Exception):
    pass


@dataclass
class Defaults:
    interval: int = 300
    grace: int = 300
    timeout: int = 10
    failures_before_alert: int = 2
    priority: int = 0
    remind_after: int = 0


@dataclass
class Config:
    path: str = ""
    listen_host: str = "127.0.0.1"
    listen_port: int = 8787
    public_url: str = "http://127.0.0.1:8787"
    tick: float = 15.0
    run_as: str = "watchdog"
    events_kept: int = 100
    db_path: str = "watchdog.db"
    pushover_token: str = ""
    pushover_user: str = ""
    pushover_api: str = PUSHOVER_API
    pushover_retry: int = 300
    pushover_expire: int = 86400
    defaults: Defaults = field(default_factory=Defaults)

    def ping_url(self, token: str) -> str:
        return f"{self.public_url.rstrip('/')}/ping/{token}"


def read_env_file(path: str | os.PathLike) -> dict:
    """Parse KEY=VALUE lines (no shell evaluation). Keys are lower-cased."""
    values = {}
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip().lower()] = value
    return values


def find_config(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    if os.environ.get("WATCHDOG_CONFIG"):
        return os.environ["WATCHDOG_CONFIG"]
    for candidate in SEARCH_PATHS:
        if os.path.exists(candidate):
            return candidate
    return None


def _resolve(base: Path, value: str) -> str:
    p = Path(value).expanduser()
    return str(p if p.is_absolute() else (base / p))


def load(path: str | None = None) -> Config:
    found = find_config(path)
    if not found:
        raise ConfigError(
            "no config file found; pass -c, set WATCHDOG_CONFIG, or create "
            + " or ".join(SEARCH_PATHS)
        )
    if not os.path.exists(found):
        raise ConfigError(f"config file not found: {found}")

    ini = configparser.ConfigParser(inline_comment_prefixes=(";", "#"), interpolation=None)
    ini.read(found)
    base = Path(found).resolve().parent
    cfg = Config(path=found)

    try:
        if ini.has_section("server"):
            s = ini["server"]
            listen = s.get("listen", "127.0.0.1:8787")
            host, _, port = listen.rpartition(":")
            cfg.listen_host = host or "127.0.0.1"
            cfg.listen_port = int(port)
            cfg.public_url = s.get("public_url", f"http://{cfg.listen_host}:{cfg.listen_port}")
            cfg.tick = s.getfloat("tick", cfg.tick)
            cfg.run_as = s.get("user", cfg.run_as)
            cfg.events_kept = s.getint("events_kept", cfg.events_kept)

        if ini.has_section("database"):
            cfg.db_path = ini["database"].get("path", cfg.db_path)
        cfg.db_path = _resolve(base, cfg.db_path)

        env = {}
        if ini.has_section("pushover"):
            p = ini["pushover"]
            if p.get("env_file"):
                env_path = _resolve(base, p["env_file"])
                try:
                    env.update(read_env_file(env_path))
                except OSError:
                    # Missing, or root-only while running as the service user:
                    # systemd passes the same keys via EnvironmentFile= instead.
                    pass
            cfg.pushover_api = p.get("api_url", cfg.pushover_api)
            cfg.pushover_retry = p.getint("retry", cfg.pushover_retry)
            cfg.pushover_expire = p.getint("expire", cfg.pushover_expire)
        # Process environment (e.g. systemd EnvironmentFile=) wins over the file.
        for key, value in os.environ.items():
            if key.lower() in ("pushover_token", "pushover_user"):
                env[key.lower()] = value
        cfg.pushover_token = env.get("pushover_token", "")
        cfg.pushover_user = env.get("pushover_user", "")

        if ini.has_section("defaults"):
            d = ini["defaults"]
            for name in vars(cfg.defaults):
                if name in d:
                    setattr(cfg.defaults, name, d.getint(name))
    except ValueError as exc:
        raise ConfigError(f"{found}: {exc}") from exc

    return cfg
