import socket
import unittest

from watchdogd.engine import Engine

from .helpers import (Clock, FakeNotifier, StubSite, pull_check, push_check, temp_store,
                      make_config)

TOKEN = "a" * 32


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.store = temp_store(self)
        self.clock = Clock()
        self.notifier = FakeNotifier()
        self.engine = Engine(self.store, self.notifier, make_config(), clock=self.clock,
                             inline_probes=True)

    def tick(self, advance=0):
        self.clock.advance(advance)
        self.engine.tick()

    def state(self, check_id):
        return self.store.get_state(check_id)


class PushCheckTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.cid = push_check(self.store, token=TOKEN)  # interval 300, grace 60

    def test_ok_pings_never_alert(self):
        for _ in range(5):
            self.assertTrue(self.engine.handle_ping(TOKEN, "ok"))
            self.tick(200)
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self.state(self.cid)["status"], "up")

    def test_fail_alerts_once_with_body_then_recovers(self):
        self.engine.handle_ping(TOKEN, "ok")
        self.engine.handle_ping(TOKEN, "fail", "disk full on /var")
        self.tick()
        self.engine.handle_ping(TOKEN, "fail", "still broken")
        self.tick(10)
        self.assertEqual(self.notifier.titles(), ["DOWN: job"])
        self.assertIn("disk full on /var", self.notifier.sent[0]["message"])
        self.assertEqual(self.notifier.sent[0]["priority"], 1)

        self.engine.handle_ping(TOKEN, "ok")
        self.tick(10)
        self.assertEqual(self.notifier.titles(), ["DOWN: job", "UP: job"])
        self.assertEqual(self.notifier.sent[1]["priority"], 0)

    def test_silence_alerts_once(self):
        self.engine.handle_ping(TOKEN, "ok")
        self.tick(359)
        self.assertEqual(self.notifier.sent, [])
        self.tick(2)
        self.tick(600)
        self.assertEqual(self.notifier.titles(), ["DOWN: job"])
        self.assertIn("no ping for", self.notifier.sent[0]["message"])

    def test_never_pinged_goes_down_after_interval_plus_grace(self):
        self.tick()
        self.assertEqual(self.state(self.cid)["status"], "new")
        self.tick(361)
        self.assertEqual(self.notifier.titles(), ["DOWN: job"])
        self.assertIn("never pinged", self.notifier.sent[0]["message"])

    def test_start_without_finish(self):
        self.engine.handle_ping(TOKEN, "start")
        self.tick(30)
        self.assertEqual(self.notifier.sent, [])
        self.tick(31)  # grace = 60
        self.assertEqual(self.notifier.titles(), ["DOWN: job"])
        self.assertIn("never finished", self.notifier.sent[0]["message"])

    def test_start_then_ok_is_fine(self):
        self.engine.handle_ping(TOKEN, "start")
        self.tick(30)
        self.engine.handle_ping(TOKEN, "ok")
        self.tick(100)
        self.assertEqual(self.notifier.sent, [])

    def test_exit_codes(self):
        self.engine.handle_ping(TOKEN, 0)
        self.tick()
        self.assertEqual(self.state(self.cid)["status"], "up")
        self.engine.handle_ping(TOKEN, 3, "Traceback: boom")
        self.tick()
        self.assertEqual(self.notifier.titles(), ["DOWN: job"])
        self.assertIn("exit code 3", self.notifier.sent[0]["message"])
        self.assertIn("Traceback: boom", self.notifier.sent[0]["message"])

    def test_unknown_token(self):
        self.assertFalse(self.engine.handle_ping("b" * 32, "ok"))

    def test_paused_check_does_not_alert_until_resumed(self):
        self.store.set_paused([self.cid], self.clock() + 3600)
        self.engine.handle_ping(TOKEN, "fail", "broken")
        self.tick(10)
        self.assertEqual(self.notifier.sent, [])
        self.store.set_paused([self.cid], None)
        self.tick(10)
        self.assertEqual(self.notifier.titles(), ["DOWN: job"])

    def test_recovered_during_pause_sends_nothing(self):
        self.store.set_paused([self.cid], self.clock() + 3600)
        self.engine.handle_ping(TOKEN, "fail")
        self.tick(10)
        self.engine.handle_ping(TOKEN, "ok")
        self.tick(10)
        self.store.set_paused([self.cid], None)
        self.tick(10)
        self.assertEqual(self.notifier.sent, [])

    def test_disabled_check_never_alerts(self):
        self.store.update_check(self.cid, {"enabled": 0})
        self.engine.handle_ping(TOKEN, "fail")
        self.tick(10000)
        self.assertEqual(self.notifier.sent, [])

    def test_single_reminder(self):
        self.store.update_check(self.cid, {"remind_after_s": 600})
        self.engine.handle_ping(TOKEN, "fail", "broken")
        self.tick()
        self.tick(599)
        self.assertEqual(len(self.notifier.sent), 1)
        self.tick(2)
        self.tick(5000)
        self.assertEqual(self.notifier.titles(), ["DOWN: job", "STILL DOWN: job"])

    def test_failed_send_is_retried_and_sent_once(self):
        self.notifier.fail = True
        self.engine.handle_ping(TOKEN, "fail")
        self.tick()
        self.assertEqual(self.notifier.sent, [])
        self.notifier.fail = False
        self.tick(5)  # still inside the backoff window
        self.assertEqual(self.notifier.sent, [])
        self.tick(60)
        self.tick(60)
        self.assertEqual(self.notifier.titles(), ["DOWN: job"])

    def test_label_is_used_in_alerts(self):
        self.store.update_check(self.cid, {"label": "Nightly backup"})
        self.engine.handle_ping(TOKEN, "fail")
        self.tick()
        self.assertEqual(self.notifier.titles(), ["DOWN: Nightly backup"])

    def test_events_are_trimmed(self):
        self.engine.cfg.events_kept = 5
        for _ in range(12):
            self.engine.handle_ping(TOKEN, "ok")
        self.assertEqual(len(self.store.recent_events(self.cid, 100)), 5)


class PullCheckTests(EngineTestCase):
    def test_http_down_after_threshold_and_recovery(self):
        site = StubSite(self)
        cid = pull_check(self.store, "site", "http", url=site.url)
        self.tick()
        self.assertEqual(self.state(cid)["status"], "up")
        site.status = 503
        self.tick(60)
        self.assertEqual(self.notifier.sent, [])  # 1 failure, threshold is 2
        self.tick(60)
        self.assertEqual(self.notifier.titles(), ["DOWN: site"])
        self.assertIn("HTTP 503", self.notifier.sent[0]["message"])
        self.tick(60)
        self.assertEqual(len(self.notifier.sent), 1)
        site.status = 200
        self.tick(60)
        self.assertEqual(self.notifier.titles(), ["DOWN: site", "UP: site"])

    def test_probe_runs_only_when_due(self):
        site = StubSite(self)
        cid = pull_check(self.store, "site", "http", url=site.url)
        self.tick()
        self.tick(30)
        self.tick(29)
        self.assertEqual(len(self.store.recent_events(cid)), 1)
        self.tick(1)
        self.assertEqual(len(self.store.recent_events(cid)), 2)

    def test_http_expect_text(self):
        site = StubSite(self)
        cid = pull_check(self.store, "site", "http", url=site.url, expect_text="Welcome",
                         failures_before_alert=1)
        self.tick()
        self.assertEqual(self.state(cid)["status"], "up")
        site.body = b"<html>maintenance</html>"
        self.tick(60)
        self.assertEqual(self.state(cid)["status"], "down")
        self.assertIn("expected text not found", self.notifier.sent[0]["message"])

    def test_tcp_closed_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]  # closed again once the block exits
        cid = pull_check(self.store, "db", "tcp", host="127.0.0.1", port=port,
                         failures_before_alert=1)
        self.tick()
        self.assertEqual(self.state(cid)["status"], "down")
        self.assertIn("cannot connect", self.notifier.sent[0]["message"])

    def test_tcp_open_port(self):
        srv = socket.socket()
        self.addCleanup(srv.close)
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        cid = pull_check(self.store, "db", "tcp", host="127.0.0.1",
                         port=srv.getsockname()[1])
        self.tick()
        self.assertEqual(self.state(cid)["status"], "up")

    def test_check_deleted_before_result_is_ignored(self):
        cid = pull_check(self.store, "gone", "http", url="http://127.0.0.1:9/")
        self.store.delete_check(cid)
        self.engine.record_probe(cid, False, "boom")
        self.assertIsNone(self.state(cid))


if __name__ == "__main__":
    unittest.main()
