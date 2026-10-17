"""Storage: SQLite (stdlib) in WAL mode.

One connection per Store, serialized by a lock, in autocommit mode so the
daemon and the admin TUI (separate processes) always see each other's writes.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

SCHEMA_VERSION = 2

CHECK_COLUMNS = (
    "name", "kind", "label", "group_name", "token", "url", "expect_status", "expect_text",
    "host", "port", "interval_s", "grace_s", "timeout_s", "failures_before_alert",
    "priority", "sound", "device", "remind_after_s", "enabled", "paused_until",
)
STATE_COLUMNS = (
    "status", "reason", "since", "last_seen", "last_ok_at", "last_ping_kind", "last_ping_ok",
    "last_message", "started_at", "last_probe_at", "consecutive_failures", "alerted_status",
    "last_alert_at", "reminded", "first_seen",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT NOT NULL PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    group_name TEXT NOT NULL DEFAULT '',
    token TEXT UNIQUE,
    url TEXT,
    expect_status TEXT,
    expect_text TEXT,
    host TEXT,
    port INTEGER,
    interval_s INTEGER NOT NULL,
    grace_s INTEGER,
    timeout_s INTEGER,
    failures_before_alert INTEGER,
    priority INTEGER NOT NULL DEFAULT 0,
    sound TEXT NOT NULL DEFAULT '',
    device TEXT NOT NULL DEFAULT '',
    remind_after_s INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    paused_until REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS check_state (
    check_id INTEGER NOT NULL PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'new',
    reason TEXT NOT NULL DEFAULT '',
    since REAL,
    last_seen REAL,
    last_ok_at REAL,
    last_ping_kind TEXT,
    last_ping_ok INTEGER,
    last_message TEXT,
    started_at REAL,
    last_probe_at REAL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    alerted_status TEXT NOT NULL DEFAULT 'up',
    last_alert_at REAL,
    reminded INTEGER NOT NULL DEFAULT 0,
    first_seen REAL
);

CREATE TABLE IF NOT EXISTS check_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    ok INTEGER NOT NULL,
    message TEXT,
    source TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_check ON check_events (check_id, id);

CREATE TABLE IF NOT EXISTS alert_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id INTEGER,
    created_at REAL NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    priority INTEGER NOT NULL,
    sound TEXT NOT NULL DEFAULT '',
    device TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    sent_at REAL,
    gave_up INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
"""


class StoreError(Exception):
    pass


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn = None

    # -- low level ---------------------------------------------------------

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            parent = os.path.dirname(os.path.abspath(self.path))
            if not os.path.isdir(parent):
                raise StoreError(f"database directory does not exist: {parent}")
            try:
                conn = sqlite3.connect(self.path, timeout=10, isolation_level=None,
                                       check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=10000")
            except sqlite3.Error as exc:
                raise StoreError(f"cannot open database {self.path}: {exc}") from exc
            self._conn = conn
        return self._conn

    def execute(self, sql: str, params=()) -> int:
        """Run a write; returns lastrowid."""
        with self._lock:
            return self._connection().execute(sql, tuple(params)).lastrowid

    def query(self, sql: str, params=()) -> list:
        with self._lock:
            return [dict(r) for r in self._connection().execute(sql, tuple(params))]

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def init_schema(self):
        with self._lock:
            self._connection().executescript(SCHEMA)
            self._migrate()
            self.execute("INSERT OR IGNORE INTO meta (k, v) VALUES (?, ?)",
                         ("schema_version", str(SCHEMA_VERSION)))

    def _migrate(self):
        """Bring databases created by older versions up to the current schema."""
        cols = {r["name"] for r in self.query("PRAGMA table_info(check_events)")}
        if "source" not in cols:  # v1 -> v2: client address of each ping
            self.execute("ALTER TABLE check_events ADD COLUMN source TEXT")
        self.execute("INSERT INTO meta (k, v) VALUES ('schema_version', ?) "
                     "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (str(SCHEMA_VERSION),))

    # -- checks ------------------------------------------------------------

    def list_checks(self) -> list:
        return self.query("SELECT * FROM checks ORDER BY group_name, name")

    def get_check(self, check_id: int):
        rows = self.query("SELECT * FROM checks WHERE id = ?", (check_id,))
        return rows[0] if rows else None

    def get_check_by_token(self, token: str):
        rows = self.query("SELECT * FROM checks WHERE token = ? AND kind = 'push'", (token,))
        return rows[0] if rows else None

    def get_check_by_name(self, name: str):
        rows = self.query("SELECT * FROM checks WHERE name = ?", (name,))
        return rows[0] if rows else None

    def add_check(self, values: dict) -> int:
        now = time.time()
        cols = [c for c in CHECK_COLUMNS if c in values]
        sql = (f"INSERT INTO checks ({', '.join(cols)}, created_at, updated_at) "
               f"VALUES ({', '.join('?' * len(cols))}, ?, ?)")
        try:
            return self.execute(sql, [values[c] for c in cols] + [now, now])
        except sqlite3.IntegrityError as exc:
            raise StoreError(f"a check named {values.get('name')!r} already exists") from exc

    def update_check(self, check_id: int, values: dict):
        cols = [c for c in CHECK_COLUMNS if c in values]
        if not cols:
            return
        sql = (f"UPDATE checks SET {', '.join(c + ' = ?' for c in cols)}, updated_at = ? "
               "WHERE id = ?")
        try:
            self.execute(sql, [values[c] for c in cols] + [time.time(), check_id])
        except sqlite3.IntegrityError as exc:
            raise StoreError(f"a check named {values.get('name')!r} already exists") from exc

    def delete_check(self, check_id: int):
        with self._lock:
            self.execute("DELETE FROM check_events WHERE check_id = ?", (check_id,))
            self.execute("DELETE FROM check_state WHERE check_id = ?", (check_id,))
            self.execute("DELETE FROM alert_outbox WHERE check_id = ? AND sent_at IS NULL",
                         (check_id,))
            self.execute("DELETE FROM checks WHERE id = ?", (check_id,))

    def set_paused(self, check_ids, until):
        for cid in check_ids:
            self.execute("UPDATE checks SET paused_until = ? WHERE id = ?", (until, cid))

    # -- global notification mute ------------------------------------------

    def get_mute(self):
        """None when notifications are on, else the epoch they resume (inf = until unmuted)."""
        rows = self.query("SELECT v FROM meta WHERE k = 'mute_until'")
        if not rows:
            return None
        return float("inf") if rows[0]["v"] == "inf" else float(rows[0]["v"])

    def set_mute(self, until):
        """until: epoch seconds, float('inf') for indefinitely, or None to unmute."""
        if until is None:
            self.execute("DELETE FROM meta WHERE k = 'mute_until'")
        else:
            value = "inf" if until == float("inf") else repr(float(until))
            self.execute("INSERT INTO meta (k, v) VALUES ('mute_until', ?) "
                         "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (value,))

    # -- state -------------------------------------------------------------

    def all_states(self) -> dict:
        return {r["check_id"]: r for r in self.query("SELECT * FROM check_state")}

    def get_state(self, check_id: int):
        rows = self.query("SELECT * FROM check_state WHERE check_id = ?", (check_id,))
        return rows[0] if rows else None

    def save_state(self, check_id: int, state: dict):
        cols = [c for c in STATE_COLUMNS if c in state]
        self.execute(
            f"INSERT INTO check_state (check_id, {', '.join(cols)}) "
            f"VALUES (?, {', '.join('?' * len(cols))}) "
            f"ON CONFLICT(check_id) DO UPDATE SET "
            + ", ".join(f"{c} = excluded.{c}" for c in cols),
            [check_id] + [state[c] for c in cols])

    def reset_state(self, check_id: int):
        self.execute("DELETE FROM check_state WHERE check_id = ?", (check_id,))

    def purge_orphans(self):
        self.execute("DELETE FROM check_state WHERE check_id NOT IN (SELECT id FROM checks)")
        self.execute("DELETE FROM check_events WHERE check_id NOT IN (SELECT id FROM checks)")

    # -- events ------------------------------------------------------------

    def add_event(self, check_id: int, ts: float, kind: str, ok: bool, message: str, keep: int,
                  source: str | None = None):
        with self._lock:
            self.execute(
                "INSERT INTO check_events (check_id, ts, kind, ok, message, source) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (check_id, ts, kind, 1 if ok else 0, message, source))
            self.execute(
                "DELETE FROM check_events WHERE check_id = ? AND id NOT IN "
                "(SELECT id FROM check_events WHERE check_id = ? ORDER BY id DESC LIMIT ?)",
                (check_id, check_id, int(keep)))

    def recent_events(self, check_id: int, limit: int = 20) -> list:
        return self.query(
            "SELECT * FROM check_events WHERE check_id = ? ORDER BY id DESC LIMIT ?",
            (check_id, int(limit)))

    # -- alert outbox ------------------------------------------------------

    def queue_alert(self, check_id, title, message, priority, sound="", device="",
                    created_at=None) -> int:
        return self.execute(
            "INSERT INTO alert_outbox (check_id, created_at, title, message, priority, sound, "
            "device) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (check_id, created_at or time.time(), title[:250], message, priority,
             sound or "", device or ""))

    def pending_alerts(self) -> list:
        return self.query(
            "SELECT * FROM alert_outbox WHERE sent_at IS NULL AND gave_up = 0 ORDER BY id")

    def mark_alert(self, alert_id: int, *, sent: bool, attempts: int, error: str = "",
                   give_up: bool = False):
        self.execute(
            "UPDATE alert_outbox SET sent_at = ?, attempts = ?, last_error = ?, gave_up = ? "
            "WHERE id = ?",
            (time.time() if sent else None, attempts, error[:1024] or None,
             1 if give_up else 0, alert_id))

    def purge_old_alerts(self, older_than: float):
        self.execute("DELETE FROM alert_outbox WHERE created_at < ? AND "
                     "(sent_at IS NOT NULL OR gave_up = 1)", (older_than,))
