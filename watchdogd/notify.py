"""Pushover client (stdlib urllib).

Equivalent to:
  curl -s --form-string "token=..." --form-string "user=..." \
       --form-string "message=..." https://api.pushover.net/1/messages.json
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from . import __version__

MAX_TITLE = 250
MAX_MESSAGE = 1024


class NotifyError(Exception):
    pass


class Pushover:
    def __init__(self, token: str, user: str, api_url: str, retry: int = 300,
                 expire: int = 86400, timeout: float = 15):
        self.token, self.user, self.api_url = token, user, api_url
        self.retry, self.expire, self.timeout = retry, expire, timeout

    @classmethod
    def from_config(cls, cfg):
        return cls(cfg.pushover_token, cfg.pushover_user, cfg.pushover_api,
                   cfg.pushover_retry, cfg.pushover_expire)

    @property
    def configured(self) -> bool:
        return bool(self.token and self.user)

    def send(self, title: str, message: str, priority: int = 0, sound: str = "",
             device: str = "") -> None:
        if not self.configured:
            raise NotifyError("Pushover token/user not configured")
        fields = {
            "token": self.token,
            "user": self.user,
            "title": title[:MAX_TITLE],
            "message": (message or title)[:MAX_MESSAGE],
            "priority": str(priority),
        }
        if priority == 2:
            fields["retry"] = str(self.retry)
            fields["expire"] = str(self.expire)
        if sound:
            fields["sound"] = sound
        if device:
            fields["device"] = device
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(self.api_url, data=data,
                                     headers={"User-Agent": f"watchdogd/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read(65536)
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode(errors="replace")
            raise NotifyError(f"Pushover HTTP {exc.code}: {_errors(detail)}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise NotifyError(f"Pushover request failed: {getattr(exc, 'reason', exc)}") from exc
        if _status(body) != 1:
            detail = _errors(body.decode(errors="replace"))
            raise NotifyError(f"Pushover rejected message: {detail}")


def _status(body: bytes):
    try:
        return json.loads(body).get("status")
    except (ValueError, AttributeError):
        return None


def _errors(text: str) -> str:
    try:
        errs = json.loads(text).get("errors")
        if errs:
            return "; ".join(map(str, errs))
    except (ValueError, AttributeError):
        pass
    return text[:200]
