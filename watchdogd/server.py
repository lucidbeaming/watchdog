"""HTTP receiver for pings and the health endpoint (stdlib http.server).

Routes (GET or POST; a POST body becomes the event message):
  /ping/<token>              ok
  /ping/<token>/ok           ok
  /ping/<token>/fail         failure
  /ping/<token>/start        job started (must be followed by ok/fail)
  /ping/<token>/<exit-code>  0 = ok, anything else = failure
  /healthz                   200 if the scheduler ran recently
"""

from __future__ import annotations

import logging
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__

log = logging.getLogger("watchdogd.http")

MAX_BODY = 64 * 1024
MAX_MESSAGE = 2048
PING_RE = re.compile(r"^/ping/([A-Za-z0-9_-]{8,64})(?:/(ok|fail|start|\d{1,3}))?/?$")


class Handler(BaseHTTPRequestHandler):
    server_version = f"watchdogd/{__version__}"
    sys_version = ""

    def _reply(self, code: int, text: str):
        body = (text + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _body(self) -> str:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return ""
        data = self.rfile.read(min(length, MAX_BODY))
        # Keep the tail: for job output the last lines are the useful ones.
        return data.decode("utf-8", errors="replace")[-MAX_MESSAGE:]

    def _handle(self):
        engine = self.server.engine
        path = self.path.split("?", 1)[0]

        if path in ("/healthz", "/healthz/"):
            last = engine.last_tick
            limit = max(120.0, 4 * engine.cfg.tick)
            if last is not None and time.time() - last < limit:
                return self._reply(200, "ok")
            return self._reply(503, "scheduler not running")

        m = PING_RE.match(path)
        if not m:
            return self._reply(404, "not found")
        token, kind = m.group(1), m.group(2) or "ok"
        if kind.isdigit():
            kind = int(kind)
        message = self._body() if self.command == "POST" else ""
        try:
            found = engine.handle_ping(token, kind, message)
        except Exception:
            log.exception("handling ping failed")
            return self._reply(500, "error")
        if not found:
            return self._reply(404, "not found")
        return self._reply(200, "OK")

    do_GET = do_POST = do_HEAD = _handle

    def log_message(self, fmt, *args):
        # Tokens are secrets: never log full ping paths.
        msg = fmt % args
        msg = re.sub(r"/ping/([A-Za-z0-9_-]{4})[A-Za-z0-9_-]*", r"/ping/\1…", msg)
        log.debug("%s %s", self.address_string(), msg)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, engine):
        self.engine = engine
        super().__init__(address, Handler)


def make_server(host: str, port: int, engine) -> Server:
    return Server((host, port), engine)
