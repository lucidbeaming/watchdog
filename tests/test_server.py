import threading
import unittest
import urllib.error
import urllib.request

from watchdogd.engine import Engine
from watchdogd.server import make_server

from .helpers import FakeNotifier, make_config, push_check, temp_store

TOKEN = "c" * 32


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.store = temp_store(self)
        self.cid = push_check(self.store, token=TOKEN)
        self.notifier = FakeNotifier()
        self.engine = Engine(self.store, self.notifier, make_config(), inline_probes=True)
        self.httpd = make_server("127.0.0.1", 0, self.engine)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def request(self, path, data=None):
        try:
            with urllib.request.urlopen(self.base + path, data=data, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def events(self):
        return self.store.recent_events(self.cid)

    def test_get_ping(self):
        self.assertEqual(self.request(f"/ping/{TOKEN}"), (200, "OK\n"))
        self.assertEqual(self.events()[0]["kind"], "ok")

    def test_kinds_and_trailing_slash(self):
        for suffix, kind in (("/ok/", "ok"), ("/start", "start"), ("/fail", "fail"),
                             ("/0", "ok"), ("/2", "fail")):
            self.assertEqual(self.request(f"/ping/{TOKEN}{suffix}")[0], 200, suffix)
            self.assertEqual(self.events()[0]["kind"], kind, suffix)

    def test_post_body_becomes_message(self):
        self.request(f"/ping/{TOKEN}/fail", data=b"line1\nERROR: disk full")
        self.assertIn("ERROR: disk full", self.events()[0]["message"])
        self.engine.send_pending()
        self.assertIn("ERROR: disk full", self.notifier.sent[0]["message"])

    def test_long_body_keeps_tail(self):
        body = ("x" * 5000 + "THE END").encode()
        self.request(f"/ping/{TOKEN}/fail", data=body)
        msg = self.events()[0]["message"]
        self.assertTrue(msg.endswith("THE END"))
        self.assertLessEqual(len(msg), 2048)

    def test_unknown_token_and_bad_paths_404(self):
        self.assertEqual(self.request(f"/ping/{'d' * 32}")[0], 404)
        self.assertEqual(self.request(f"/ping/{TOKEN}/bogus")[0], 404)
        self.assertEqual(self.request("/ping/short")[0], 404)
        self.assertEqual(self.request("/")[0], 404)
        self.assertEqual(self.events(), [])

    def test_healthz(self):
        self.assertEqual(self.request("/healthz")[0], 503)
        self.engine.tick()
        self.assertEqual(self.request("/healthz"), (200, "ok\n"))


if __name__ == "__main__":
    unittest.main()
