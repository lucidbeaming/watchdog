"""Check definitions: field specs (shared by the TUI and validation) and probes."""

from __future__ import annotations

import re
import secrets
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import __version__

KINDS = ("push", "http", "tcp")
KIND_HELP = {
    "push": "the monitored job/host pings the watchdog (dead-man's switch)",
    "http": "the watchdog fetches a URL (e.g. a web server's index page)",
    "tcp": "the watchdog opens a TCP connection to host:port",
}
PRIORITIES = (-2, -1, 0, 1, 2)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
STATUS_RE = re.compile(r"^\d{3}(-\d{3})?(,\d{3}(-\d{3})?)*$")
USER_AGENT = f"watchdogd/{__version__}"


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kinds: tuple = KINDS
    type: str = "str"  # str | int | choice | bool
    choices: tuple = ()
    required: bool = False
    help: str = ""
    max_len: int = 255


FIELDS = (
    Field("name", "Name", required=True, max_len=100,
          help="unique id: letters, digits, . _ -"),
    Field("kind", "Type", type="choice", choices=KINDS, required=True),
    Field("label", "Label", help="human-readable description used in alerts"),
    Field("group_name", "Group", max_len=100, help="for filtering and pausing together"),
    Field("url", "URL", kinds=("http",), required=True, max_len=2048,
          help="http:// or https:// address to fetch"),
    Field("expect_status", "Expect status", kinds=("http",),
          help="e.g. 200 or 200-399 or 200,301"),
    Field("expect_text", "Expect text", kinds=("http",),
          help="optional: page must contain this text"),
    Field("host", "Host", kinds=("tcp",), required=True),
    Field("port", "Port", kinds=("tcp",), type="int", required=True),
    Field("interval_s", "Interval (s)", type="int", required=True,
          help="push: expected ping period; http/tcp: probe period"),
    Field("grace_s", "Grace (s)", kinds=("push",), type="int",
          help="extra slack before a missing ping counts as down"),
    Field("timeout_s", "Timeout (s)", kinds=("http", "tcp"), type="int"),
    Field("failures_before_alert", "Failures before alert", kinds=("http", "tcp"), type="int",
          help="consecutive failed probes before going down"),
    Field("priority", "Priority", type="choice", choices=PRIORITIES,
          help="Pushover: -2 silent .. 2 emergency (repeats until acknowledged)"),
    Field("sound", "Sound", max_len=50, help="optional Pushover sound name"),
    Field("device", "Device", max_len=100, help="optional Pushover device name"),
    Field("remind_after_s", "Remind after (s)", type="int",
          help="send one 'still down' reminder after this long; 0 = off"),
    Field("enabled", "Enabled", type="bool"),
)
FIELD_BY_NAME = {f.name: f for f in FIELDS}


def fields_for(kind: str) -> list:
    return [f for f in FIELDS if kind in f.kinds]


def new_token() -> str:
    return secrets.token_hex(16)


def blank_check(defaults, kind: str = "push") -> dict:
    """A new check pre-filled from config defaults."""
    return {
        "name": "", "kind": kind, "label": "", "group_name": "",
        "url": "", "expect_status": "200-399", "expect_text": "",
        "host": "", "port": None,
        "interval_s": defaults.interval, "grace_s": defaults.grace,
        "timeout_s": defaults.timeout,
        "failures_before_alert": defaults.failures_before_alert,
        "priority": defaults.priority, "sound": "", "device": "",
        "remind_after_s": defaults.remind_after, "enabled": 1,
    }


def validate(values: dict) -> tuple[dict, dict]:
    """Return (clean, errors). Fields that don't apply to the kind are cleared."""
    clean, errors = {}, {}
    kind = values.get("kind")
    if kind not in KINDS:
        return {}, {"kind": f"must be one of {', '.join(KINDS)}"}

    for f in FIELDS:
        raw = values.get(f.name)
        if kind not in f.kinds:
            clean[f.name] = None
            continue
        if isinstance(raw, str):
            raw = raw.strip()
        if raw in (None, ""):
            if f.required:
                errors[f.name] = "required"
            clean[f.name] = None if f.type in ("int", "choice") else ("" if f.type == "str" else 0)
            continue
        try:
            if f.type == "int":
                v = int(raw)
                if v < 0:
                    raise ValueError
                clean[f.name] = v
            elif f.type == "choice":
                v = type(f.choices[0])(raw)
                if v not in f.choices:
                    raise ValueError
                clean[f.name] = v
            elif f.type == "bool":
                clean[f.name] = 1 if str(raw).lower() in ("1", "true", "yes", "y", "on") else 0
            else:
                v = str(raw)
                if len(v) > f.max_len:
                    errors[f.name] = f"at most {f.max_len} characters"
                clean[f.name] = v
        except (ValueError, TypeError):
            errors[f.name] = "invalid value"
            clean[f.name] = None

    if clean.get("name") and not NAME_RE.match(clean["name"]):
        errors["name"] = "use letters, digits, . _ - (max 100)"
    if kind == "http":
        if clean.get("url") and not re.match(r"^https?://[^\s/]+", clean["url"]):
            errors["url"] = "must start with http:// or https://"
        if clean.get("expect_status") and not STATUS_RE.match(clean["expect_status"]):
            errors["expect_status"] = "e.g. 200 or 200-399 or 200,301"
        if not clean.get("expect_status"):
            clean["expect_status"] = "200-399"
    if kind == "tcp" and clean.get("port") is not None and not 1 <= clean["port"] <= 65535:
        errors["port"] = "1-65535"
    if clean.get("interval_s") is not None and clean["interval_s"] < 10:
        errors["interval_s"] = "at least 10 seconds"
    if kind in ("http", "tcp"):
        if not clean.get("timeout_s"):
            clean["timeout_s"] = 10
        if not clean.get("failures_before_alert"):
            clean["failures_before_alert"] = 1
    if kind == "push" and clean.get("grace_s") is None:
        clean["grace_s"] = 0
    if clean.get("priority") is None:
        clean["priority"] = 0
    if clean.get("remind_after_s") is None:
        clean["remind_after_s"] = 0
    return clean, errors


def status_matches(code: int, spec: str) -> bool:
    for part in (spec or "200-399").split(","):
        lo, _, hi = part.partition("-")
        if int(lo) <= code <= int(hi or lo):
            return True
    return False


def probe_http(check: dict) -> tuple[bool, str]:
    """Fetch the URL; returns (ok, message)."""
    url = check["url"]
    timeout = check.get("timeout_s") or 10
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    body = b""
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl.create_default_context()) as resp:
            code = resp.status
            if check.get("expect_text"):
                body = resp.read(1024 * 1024)
    except urllib.error.HTTPError as exc:
        code = exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"request failed: {reason}"

    if not status_matches(code, check.get("expect_status") or "200-399"):
        return False, f"HTTP {code} (expected {check.get('expect_status') or '200-399'})"
    text = check.get("expect_text")
    if text and text.encode() not in body:
        return False, f"HTTP {code}, but expected text not found"
    return True, f"HTTP {code}"


def probe_tcp(check: dict) -> tuple[bool, str]:
    host, port = check["host"], int(check["port"])
    timeout = check.get("timeout_s") or 10
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"connected to {host}:{port}"
    except OSError as exc:
        return False, f"cannot connect to {host}:{port}: {exc.strerror or exc}"


PROBES = {"http": probe_http, "tcp": probe_tcp}


def describe_target(check: dict) -> str:
    if check["kind"] == "http":
        return check.get("url") or ""
    if check["kind"] == "tcp":
        return f"{check.get('host')}:{check.get('port')}"
    return f"ping every {fmt_duration(check.get('interval_s'))}"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m" if not s or m >= 10 else f"{m}m {s}s"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if h else f"{d}d"


def parse_duration(text: str) -> int:
    """'90', '30s', '15m', '2h', '1d', '1h30m' -> seconds."""
    text = text.strip().lower().replace(" ", "")
    if text.isdigit():
        return int(text)
    parts = re.findall(r"(\d+)([smhdw])", text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise ValueError(f"bad duration: {text!r} (use e.g. 30m, 2h, 1d)")
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    return sum(int(n) * mult[u] for n, u in parts)
