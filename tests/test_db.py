import os
import sqlite3
import tempfile
import unittest

from watchdogd.db import SCHEMA_VERSION, Store

V1_EVENTS = """
CREATE TABLE meta (k TEXT NOT NULL PRIMARY KEY, v TEXT NOT NULL);
INSERT INTO meta VALUES ('schema_version', '1');
CREATE TABLE check_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    ok INTEGER NOT NULL,
    message TEXT
);
INSERT INTO check_events (check_id, ts, kind, ok, message) VALUES (1, 1.0, 'ok', 1, 'old');
"""


class MigrationTests(unittest.TestCase):
    def test_v1_database_gains_source_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            conn = sqlite3.connect(path)
            conn.executescript(V1_EVENTS)
            conn.close()

            store = Store(path)
            store.init_schema()
            store.init_schema()  # idempotent
            [old] = store.recent_events(1)
            self.assertEqual(old["message"], "old")
            self.assertIsNone(old["source"])
            store.add_event(1, 2.0, "ok", True, "", 100, source="203.0.113.9")
            self.assertEqual(store.recent_events(1)[0]["source"], "203.0.113.9")
            version = store.query("SELECT v FROM meta WHERE k = 'schema_version'")[0]["v"]
            self.assertEqual(version, str(SCHEMA_VERSION))
            store.close()


if __name__ == "__main__":
    unittest.main()
