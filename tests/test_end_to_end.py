"""Run the real daemon (`watchdog serve`) and drive it with the shell clients."""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from watchdogd.db import Store

from .helpers import StubPushover

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "client")
TOKEN = "e" * 32


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(shutil.which("curl") and os.name == "posix", "needs curl and a POSIX shell")
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.pushover = StubPushover(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.port = free_port()
        self.ini = os.path.join(self.dir, "watchdog.ini")
        with open(self.ini, "w") as f:
            f.write(f"""
[server]
listen = 127.0.0.1:{self.port}
public_url = http://127.0.0.1:{self.port}
tick = 0.2
[database]
path = wd.db
[pushover]
api_url = {self.pushover.url}
""")
        self.env = dict(os.environ, PYTHONPATH=ROOT, PUSHOVER_TOKEN="tok",
                        PUSHOVER_USER="usr")
        self.watchdog([ "init-db"])
        store = Store(os.path.join(self.dir, "wd.db"))
        store.add_check(dict(name="backup", kind="push", token=TOKEN, interval_s=300,
                             grace_s=60, priority=1, enabled=1))
        store.close()

        self.daemon = subprocess.Popen(
            [sys.executable, "-m", "watchdogd", "-c", self.ini, "serve"], env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_daemon)
        self.base = f"http://127.0.0.1:{self.port}"
        self.wait_for(lambda: self.get("/healthz") == 200, "daemon to become healthy")

    def stop_daemon(self):
        self.daemon.terminate()
        try:
            self.daemon.wait(5)
        finally:
            self.daemon.stderr.close()

    def watchdog(self, args):
        return subprocess.run([sys.executable, "-m", "watchdogd", "-c", self.ini] + args,
                              env=self.env, capture_output=True, text=True, timeout=30)

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=2) as r:
                return r.status
        except Exception:
            return None

    def wait_for(self, cond, what, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}")

    def client(self, *args, stdin=None):
        return subprocess.run(list(args), input=stdin, capture_output=True, text=True,
                              timeout=30, env=dict(os.environ, PATH=CLIENT + ":" +
                                                   os.environ["PATH"]))

    def test_wd_run_failure_alerts_with_output_then_recovers(self):
        url = f"{self.base}/ping/{TOKEN}"
        r = self.client(os.path.join(CLIENT, "wd-run"), url, "--", "sh", "-c",
                        "echo working; echo 'ERROR: tape not found' >&2; exit 4")
        self.assertEqual(r.returncode, 4)
        self.assertIn("ERROR: tape not found", r.stdout)  # output still visible for cron mail
        self.wait_for(lambda: self.pushover.messages, "down alert")
        down = self.pushover.messages[0]
        self.assertEqual(down["title"], "DOWN: backup")
        self.assertIn("exit code 4", down["message"])
        self.assertIn("ERROR: tape not found", down["message"])
        self.assertEqual((down["token"], down["user"], down["priority"]), ("tok", "usr", "1"))

        r = self.client(os.path.join(CLIENT, "wd-run"), url, "--", "true")
        self.assertEqual(r.returncode, 0)
        self.wait_for(lambda: len(self.pushover.messages) == 2, "recovery alert")
        self.assertEqual(self.pushover.messages[1]["title"], "UP: backup")

        status = self.watchdog(["status"])
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn("backup", status.stdout)

    def test_probe_script_reports_failures(self):
        script = f"""
. {CLIENT}/wd-probes.sh
probe_cmd "always ok" -- true
probe_cmd "queue check failed" -- false
probe_disk / 100
wd_report {self.base}/ping/{TOKEN}
"""
        r = self.client("sh", "-c", script)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.wait_for(lambda: self.pushover.messages, "down alert")
        msg = self.pushover.messages[0]["message"]
        self.assertIn("queue check failed", msg)
        self.assertNotIn("always ok", msg)

    def test_wd_ping_never_fails_the_job(self):
        r = self.client(os.path.join(CLIENT, "wd-ping"), "http://127.0.0.1:9/ping/" + TOKEN)
        self.assertEqual(r.returncode, 0)
        self.assertIn("could not reach", r.stderr)

    def test_cli_pause_and_show(self):
        r = self.watchdog(["pause", "backup", "2h"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("paused backup for 2h", r.stdout)
        r = self.watchdog(["show", "backup"])
        self.assertIn(f"/ping/{TOKEN}", r.stdout)
        r = self.watchdog(["resume", "backup"])
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
