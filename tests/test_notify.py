import unittest

from watchdogd.notify import NotifyError, Pushover

from .helpers import StubPushover


class PushoverTests(unittest.TestCase):
    def setUp(self):
        self.stub = StubPushover(self)
        self.po = Pushover("tok", "usr", self.stub.url, retry=120, expire=3600)

    def test_form_fields(self):
        self.po.send("DOWN: web", "HTTP 503", priority=1, sound="siren")
        msg = self.stub.messages[0]
        self.assertEqual(msg["token"], "tok")
        self.assertEqual(msg["user"], "usr")
        self.assertEqual(msg["title"], "DOWN: web")
        self.assertEqual(msg["message"], "HTTP 503")
        self.assertEqual(msg["priority"], "1")
        self.assertEqual(msg["sound"], "siren")
        self.assertNotIn("retry", msg)

    def test_emergency_priority_adds_retry_and_expire(self):
        self.po.send("DOWN", "x", priority=2)
        self.assertEqual(self.stub.messages[0]["retry"], "120")
        self.assertEqual(self.stub.messages[0]["expire"], "3600")

    def test_message_is_truncated(self):
        self.po.send("t", "y" * 5000)
        self.assertEqual(len(self.stub.messages[0]["message"]), 1024)

    def test_rejection_raises(self):
        self.stub.reply_status = 0
        with self.assertRaisesRegex(NotifyError, "bad token"):
            self.po.send("t", "m")

    def test_unreachable_raises(self):
        po = Pushover("tok", "usr", "http://127.0.0.1:9/1/messages.json", timeout=2)
        with self.assertRaises(NotifyError):
            po.send("t", "m")

    def test_unconfigured_raises(self):
        with self.assertRaisesRegex(NotifyError, "not configured"):
            Pushover("", "", self.stub.url).send("t", "m")


if __name__ == "__main__":
    unittest.main()
