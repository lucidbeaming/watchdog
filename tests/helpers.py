"""Shared test fixtures: temp store, fake clock, stub Pushover and stub website."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from watchdogd.config import Config
from watchdogd.db import Store
from watchdogd.notify import NotifyError

T0 = 1_700_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeNotifier:
    configured = True

    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, title, message, priority=0, sound="", device=""):
        if self.fail:
            raise NotifyError("stub failure")
        self.sent.append({"title": title, "message": message, "priority": priority})

    def titles(self):
        return [m["title"] for m in self.sent]


def temp_store(testcase) -> Store:
    tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    store = Store(os.path.join(tmp.name, "test.db"))
    store.init_schema()
    testcase.addCleanup(store.close)
    return store


def make_config(**kw) -> Config:
    cfg = Config(public_url="https://watchdog.example.com/watchdog", events_kept=100)
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def push_check(store, name="job", token="a" * 32, **kw) -> int:
    values = dict(name=name, kind="push", token=token, interval_s=300, grace_s=60, priority=1,
                  enabled=1)
    values.update(kw)
    return store.add_check(values)


def pull_check(store, name, kind, **kw) -> int:
    values = dict(name=name, kind=kind, interval_s=60, timeout_s=2, failures_before_alert=2,
                  priority=0, enabled=1, expect_status="200-399")
    values.update(kw)
    return store.add_check(values)


class _Server:
    """Run a ThreadingHTTPServer on a free port for the duration of a test."""

    def __init__(self, testcase, handler):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        testcase.addCleanup(self.close)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class StubSite(_Server):
    """A web server whose status code and page body tests can change."""

    def __init__(self, testcase):
        site = self
        self.status, self.body = 200, b"<html><title>Welcome</title></html>"

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(site.status)
                self.send_header("Content-Length", str(len(site.body)))
                self.end_headers()
                self.wfile.write(site.body)

            def log_message(self, *a):
                pass

        super().__init__(testcase, H)
        self.url = f"http://127.0.0.1:{self.port}/"


class StubPushover(_Server):
    """Records form posts like api.pushover.net/1/messages.json."""

    def __init__(self, testcase):
        stub = self
        self.messages, self.reply_status = [], 1

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                form = urllib.parse.parse_qs(self.rfile.read(length).decode())
                stub.messages.append({k: v[0] for k, v in form.items()})
                body = json.dumps({"status": stub.reply_status,
                                   "errors": [] if stub.reply_status == 1 else ["bad token"]})
                self.send_response(200 if stub.reply_status == 1 else 400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *a):
                pass

        super().__init__(testcase, H)
        self.url = f"http://127.0.0.1:{self.port}/1/messages.json"
