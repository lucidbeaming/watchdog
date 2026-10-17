"""Drive the curses TUI through a pseudo-terminal with real keystrokes."""

import fcntl
import os
import struct
import sys
import tempfile
import termios
import threading
import time
import unittest

from watchdogd.db import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RIGHT, F2, ESC, TAB, ENTER, CTRL_S = "\x1bOC", "\x1bOQ", "\x1b", "\t", "\r", "\x13"


@unittest.skipUnless(os.name == "posix", "needs a pty")
class TuiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.ini = os.path.join(self.dir, "watchdog.ini")
        with open(self.ini, "w") as f:
            f.write("[server]\npublic_url = https://watchdog.example.com/watchdog\n"
                    "[database]\npath = wd.db\n")
        self.output = b""

    def start(self):
        import pty
        pid, fd = pty.fork()
        if pid == 0:
            os.environ.update(TERM="xterm-256color", PYTHONPATH=ROOT, ESCDELAY="25")
            os.execv(sys.executable, [sys.executable, "-m", "watchdogd", "-c", self.ini, "tui"])
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        self.pid, self.fd = pid, fd
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.addCleanup(self._cleanup)
        self.wait_for_text("watchdog")

    def _read(self):
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return
            if not data:
                return
            self.output += data

    def _cleanup(self):
        try:
            os.kill(self.pid, 9)
            os.waitpid(self.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        os.close(self.fd)

    def send(self, *keys, delay=0.15):
        for k in keys:
            os.write(self.fd, k.encode())
            time.sleep(delay)

    def type(self, text):
        self.send(text, delay=0.2)

    def wait_for_text(self, text, timeout=8):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if text.encode() in self.output:
                return
            time.sleep(0.05)
        self.fail(f"{text!r} never appeared on screen")

    def wait_exit(self, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                return os.waitstatus_to_exitcode(status)
            time.sleep(0.05)
        self.fail("TUI did not exit")

    def checks(self):
        store = Store(os.path.join(self.dir, "wd.db"))
        try:
            return store.list_checks()
        finally:
            store.close()

    def test_add_http_check(self):
        self.start()
        self.wait_for_text("No checks yet")
        self.send("a")
        self.wait_for_text("Add check")
        self.type("web-home")
        self.send(TAB, RIGHT)                      # Type: push -> http
        self.wait_for_text("Expect status")
        self.send(TAB)
        self.type("Homepage")                      # Label
        self.send(TAB)
        self.type("sites")                         # Group
        self.send(TAB)
        self.type("https://www.example.com/")      # URL
        self.send(CTRL_S)
        self.wait_for_text("added web-home")
        self.wait_for_text("https://www.example.com/")
        self.send(ESC, "q")
        self.assertEqual(self.wait_exit(), 0)

        [c] = self.checks()
        self.assertEqual((c["name"], c["kind"], c["label"], c["group_name"], c["url"]),
                         ("web-home", "http", "Homepage", "sites", "https://www.example.com/"))
        self.assertEqual(c["expect_status"], "200-399")
        self.assertIsNone(c["token"])

    def test_add_push_check_shows_ping_url(self):
        self.start()
        self.send("a")
        self.wait_for_text("Add check")
        self.type("nightly-backup")
        self.send(F2)
        self.wait_for_text("added nightly-backup")
        [c] = self.checks()
        self.assertEqual(c["kind"], "push")
        self.assertEqual(len(c["token"]), 32)
        self.wait_for_text(f"https://watchdog.example.com/watchdog/ping/{c['token']}")
        self.send(ESC, "q")
        self.assertEqual(self.wait_exit(), 0)

    def test_validation_keeps_form_open(self):
        self.start()
        self.send("a")
        self.wait_for_text("Add check")
        self.send(CTRL_S)                          # name is empty
        self.wait_for_text("required")
        self.wait_for_text("need attention")
        self.send(ESC)                             # unchanged form: no discard prompt
        self.wait_for_text("No checks yet")
        self.send("q")
        self.assertEqual(self.wait_exit(), 0)
        self.assertEqual(self.checks(), [])

    def test_delete_with_confirmation(self):
        store = Store(os.path.join(self.dir, "wd.db"))
        store.init_schema()
        store.add_check(dict(name="old", kind="push", token="f" * 32, interval_s=60, enabled=1))
        store.close()
        self.start()
        self.wait_for_text("old")
        self.send("d")
        self.wait_for_text("Delete old")
        self.send("n")
        self.assertEqual(len(self.checks()), 1)
        self.send("d")
        self.send("y")
        self.wait_for_text("deleted old")
        self.send("q")
        self.assertEqual(self.wait_exit(), 0)
        self.assertEqual(self.checks(), [])


if __name__ == "__main__":
    unittest.main()
