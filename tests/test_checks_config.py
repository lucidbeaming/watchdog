import os
import tempfile
import textwrap
import unittest
from unittest import mock

from watchdogd import checks, config
from watchdogd.config import Defaults


class ValidateTests(unittest.TestCase):
    def base(self, kind, **kw):
        v = {k: ("" if val is None else str(val))
             for k, val in checks.blank_check(Defaults(), kind).items()}
        v["enabled"] = "yes"
        v["name"] = "web-1"
        v.update(kw)
        return v

    def test_push_ok(self):
        clean, errors = checks.validate(self.base("push"))
        self.assertEqual(errors, {})
        self.assertEqual(clean["interval_s"], 300)
        self.assertIsNone(clean["url"])  # http-only fields are cleared
        self.assertEqual(clean["enabled"], 1)

    def test_http_requires_valid_url(self):
        _, errors = checks.validate(self.base("http"))
        self.assertEqual(errors.get("url"), "required")
        _, errors = checks.validate(self.base("http", url="ftp://x"))
        self.assertIn("url", errors)
        clean, errors = checks.validate(self.base("http", url="https://example.com/"))
        self.assertEqual(errors, {})
        self.assertEqual(clean["expect_status"], "200-399")

    def test_tcp_port_range(self):
        _, errors = checks.validate(self.base("tcp", host="db.example.com", port="70000"))
        self.assertIn("port", errors)
        _, errors = checks.validate(self.base("tcp", host="db.example.com", port="5432"))
        self.assertEqual(errors, {})

    def test_bad_values(self):
        _, errors = checks.validate(self.base("push", name="has space", interval_s="5",
                                              priority="7"))
        self.assertEqual(set(errors), {"name", "interval_s", "priority"})

    def test_bad_kind(self):
        _, errors = checks.validate(self.base("smtp"))
        self.assertIn("kind", errors)

    def test_status_matches(self):
        self.assertTrue(checks.status_matches(204, "200-299"))
        self.assertTrue(checks.status_matches(301, "200,301"))
        self.assertFalse(checks.status_matches(500, "200-399"))

    def test_durations(self):
        self.assertEqual(checks.parse_duration("90"), 90)
        self.assertEqual(checks.parse_duration("1h30m"), 5400)
        self.assertEqual(checks.parse_duration("2d"), 172800)
        with self.assertRaises(ValueError):
            checks.parse_duration("soon")
        self.assertEqual(checks.fmt_duration(45), "45s")
        self.assertEqual(checks.fmt_duration(3700), "1h 1m")
        self.assertEqual(checks.fmt_duration(90000), "25h")
        self.assertEqual(checks.fmt_duration(3 * 86400), "3d")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write(textwrap.dedent(text))
        return path

    def test_env_file_parsing(self):
        path = self.write("p.env", """
            # comment
            PUSHOVER_TOKEN="abc"
            export pushover_user='def'
            junk line
        """)
        self.assertEqual(config.read_env_file(path),
                         {"pushover_token": "abc", "pushover_user": "def"})

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_load_resolves_relative_paths(self):
        self.write("secrets.env", "pushover_token=t1\npushover_user=u1\n")
        path = self.write("w.ini", """
            [server]
            listen = 0.0.0.0:9000
            public_url = https://watchdog.example.com/wd/
            [database]
            path = data/wd.db
            [pushover]
            env_file = secrets.env
            [defaults]
            interval = 60   ; inline comment
        """)
        cfg = config.load(path)
        self.assertEqual((cfg.listen_host, cfg.listen_port), ("0.0.0.0", 9000))
        self.assertEqual(cfg.db_path, os.path.join(os.path.realpath(self.dir), "data", "wd.db"))
        self.assertEqual((cfg.pushover_token, cfg.pushover_user), ("t1", "u1"))
        self.assertEqual(cfg.defaults.interval, 60)
        self.assertEqual(cfg.ping_url("x" * 32),
                         "https://watchdog.example.com/wd/ping/" + "x" * 32)

    def test_process_env_wins(self):
        self.write("secrets.env", "pushover_token=fromfile\n")
        path = self.write("w.ini", "[pushover]\nenv_file = secrets.env\n")
        with mock.patch.dict(os.environ, {"PUSHOVER_TOKEN": "fromenv"}):
            self.assertEqual(config.load(path).pushover_token, "fromenv")

    def test_missing_env_file_is_not_fatal(self):
        path = self.write("w.ini", "[pushover]\nenv_file = /nonexistent/p.env\n")
        config.load(path)

    def test_bad_value(self):
        path = self.write("w.ini", "[server]\nlisten = 127.0.0.1:http\n")
        with self.assertRaises(config.ConfigError):
            config.load(path)

    def test_missing_file(self):
        with self.assertRaises(config.ConfigError):
            config.load(os.path.join(self.dir, "nope.ini"))


if __name__ == "__main__":
    unittest.main()
