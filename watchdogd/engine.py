"""Check evaluation: pings and probe results -> status -> alerts.

Alerts fire only when the alerted status changes (down, then recovered), plus an
optional single "still down" reminder. Alerts go through a DB outbox so a failed
Pushover send is retried with backoff instead of being lost.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .checks import PROBES, describe_target, fmt_duration
from .notify import NotifyError

log = logging.getLogger("watchdogd")

GIVE_UP_AFTER = 86400
HOUSEKEEPING_EVERY = 600


def _pull(check) -> bool:
    return check["kind"] in PROBES


class Engine:
    def __init__(self, store, notifier, cfg, *, clock=time.time, inline_probes=False,
                 probe_workers=8):
        self.store, self.notifier, self.cfg, self.clock = store, notifier, cfg, clock
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.executor = None if inline_probes else ThreadPoolExecutor(
            probe_workers, thread_name_prefix="probe")
        self.in_flight: set = set()
        self.last_tick: float | None = None
        self._next_send: dict = {}
        self._send_lock = threading.Lock()
        self._last_housekeeping = 0.0
        self._warned_unconfigured = False
        self._was_muted = False

    def shutdown(self):
        if self.executor:
            self.executor.shutdown(wait=False, cancel_futures=True)

    # -- state helpers -----------------------------------------------------

    def _load_state(self, check, now) -> dict:
        state = self.store.get_state(check["id"])
        if state is None:
            state = {"status": "new", "reason": "", "since": now, "first_seen": now,
                     "consecutive_failures": 0, "alerted_status": "up", "reminded": 0,
                     "last_seen": None, "last_ok_at": None, "last_ping_kind": None,
                     "last_ping_ok": None, "last_message": None, "started_at": None,
                     "last_probe_at": None, "last_alert_at": None}
        return state

    def _compute(self, check, state, now) -> tuple[str, str]:
        if _pull(check):
            fails = state["consecutive_failures"] or 0
            if fails >= (check["failures_before_alert"] or 1):
                return "down", state.get("last_message") or "probe failed"
            if fails == 0 and state.get("last_probe_at"):
                return "up", state.get("last_message") or ""
            return state["status"], state.get("reason") or ""

        interval = check["interval_s"]
        grace = check["grace_s"] or 0
        hang_limit = grace or interval
        if state.get("started_at") and now - state["started_at"] > hang_limit:
            return "down", (f"started {fmt_duration(now - state['started_at'])} ago "
                            "but never finished")
        if state.get("last_ping_ok") == 0:
            msg = (state.get("last_message") or "").strip()
            return "down", "reported failure" + (f": {msg}" if msg else "")
        ref = state.get("last_seen") or state.get("first_seen") or now
        if now - ref > interval + grace:
            if state.get("last_seen"):
                return "down", (f"no ping for {fmt_duration(now - ref)} "
                                f"(expected every {fmt_duration(interval)})")
            return "down", f"never pinged (expected every {fmt_duration(interval)})"
        if state.get("last_seen"):
            return "up", "ok"
        return "new", "waiting for first ping"

    def muted(self, now=None) -> bool:
        """Global mute: checks keep running and recording, but nothing is sent."""
        until = self.store.get_mute()
        return until is not None and until > (self.clock() if now is None else now)

    def _evaluate(self, check, state, now):
        status, reason = self._compute(check, state, now)
        if status != state["status"]:
            state["since"] = now
        state["status"], state["reason"] = status, reason

        # Paused or muted: keep tracking but don't alert. alerted_status stays put, so
        # anything still down when the pause/mute ends alerts then.
        paused = (check.get("paused_until") or 0) > now or self.muted(now)
        effective = "down" if status == "down" else "up"
        if not paused:
            if effective != state["alerted_status"]:
                if effective == "down":
                    self._queue(check, "DOWN", reason, check["priority"])
                else:
                    down_for = ""
                    if state.get("last_alert_at"):
                        down_for = f" after {fmt_duration(now - state['last_alert_at'])}"
                    self._queue(check, "UP", f"recovered{down_for}", min(check["priority"], 0))
                state["alerted_status"] = effective
                state["last_alert_at"] = now
                state["reminded"] = 0
            elif (effective == "down" and check.get("remind_after_s")
                  and not state.get("reminded")
                  and now - (state.get("last_alert_at") or now) >= check["remind_after_s"]):
                self._queue(check, "STILL DOWN",
                            f"down for {fmt_duration(now - state['last_alert_at'])}: {reason}",
                            min(check["priority"], 1))
                state["reminded"] = 1
        self.store.save_state(check["id"], state)

    def _queue(self, check, what, detail, priority):
        name = check.get("label") or check["name"]
        title = f"{what}: {name}"
        target = describe_target(check)
        message = f"{detail}\n\ncheck: {check['name']} ({check['kind']}"
        message += f" {target})" if check["kind"] != "push" else ")"
        self.store.queue_alert(check["id"], title, message, priority,
                               check.get("sound"), check.get("device"), created_at=self.clock())
        log.info("alert queued: %s - %s", title, detail.splitlines()[0] if detail else "")
        self.wake.set()

    # -- inputs ------------------------------------------------------------

    def handle_ping(self, token: str, kind, message: str = "") -> bool:
        """kind: 'ok' | 'fail' | 'start' | int exit code. Returns False for unknown token."""
        check = self.store.get_check_by_token(token)
        if check is None:
            return False
        now = self.clock()
        message = (message or "").strip()
        if isinstance(kind, int):
            ok = kind == 0
            if not ok:
                message = f"exit code {kind}" + (f"\n{message}" if message else "")
            kind = "ok" if ok else "fail"
        with self.lock:
            state = self._load_state(check, now)
            state["last_seen"] = now
            state["last_ping_kind"] = kind
            if kind == "start":
                state["started_at"] = now
            else:
                ok = kind == "ok"
                state["started_at"] = None
                state["last_ping_ok"] = 1 if ok else 0
                state["last_message"] = message
                if ok:
                    state["last_ok_at"] = now
            self.store.add_event(check["id"], now, kind, kind != "fail", message,
                                 self.cfg.events_kept)
            if check["enabled"]:
                self._evaluate(check, state, now)
            else:
                self.store.save_state(check["id"], state)
        return True

    def _run_probe(self, check, started):
        try:
            try:
                ok, message = PROBES[check["kind"]](check)
            except Exception as exc:  # a probe bug must not kill the worker
                log.exception("probe %s crashed", check["name"])
                ok, message = False, f"probe error: {exc}"
            self.record_probe(check["id"], ok, message, started)
        except Exception:
            log.exception("recording probe result for %s failed", check["name"])
        finally:
            with self.lock:
                self.in_flight.discard(check["id"])

    def record_probe(self, check_id, ok, message, started=None):
        now = self.clock()
        with self.lock:
            check = self.store.get_check(check_id)  # may have been edited or deleted
            if check is None or not _pull(check):
                return
            state = self._load_state(check, now)
            state["last_probe_at"] = started or now
            state["last_seen"] = now
            state["last_message"] = message
            if ok:
                state["consecutive_failures"] = 0
                state["last_ok_at"] = now
            else:
                state["consecutive_failures"] = (state["consecutive_failures"] or 0) + 1
            self.store.add_event(check_id, now, "probe", ok, message, self.cfg.events_kept)
            if check["enabled"]:
                self._evaluate(check, state, now)
            else:
                self.store.save_state(check_id, state)

    # -- scheduler ---------------------------------------------------------

    def tick(self):
        now = self.clock()
        for check in self.store.list_checks():
            if not check["enabled"]:
                continue
            with self.lock:
                state = self._load_state(check, now)
                if _pull(check):
                    due = (state.get("last_probe_at") is None
                           or now - state["last_probe_at"] >= check["interval_s"])
                    if due and check["id"] not in self.in_flight:
                        self.in_flight.add(check["id"])
                        if self.executor:
                            self.executor.submit(self._run_probe, check, now)
                        else:
                            self._run_probe(check, now)
                        continue
                self._evaluate(check, state, now)
        self.send_pending()
        if now - self._last_housekeeping >= HOUSEKEEPING_EVERY:
            self.store.purge_orphans()
            self.store.purge_old_alerts(now - 30 * 86400)
            self._last_housekeeping = now
        self.last_tick = now

    def send_pending(self):
        if not self._send_lock.acquire(blocking=False):
            return
        try:
            now = self.clock()
            muted = self.muted(now)
            if muted != self._was_muted:
                log.info("notifications %s", "muted" if muted else "unmuted")
                self._was_muted = muted
            if muted:
                return  # queued alerts wait until unmuted
            for alert in self.store.pending_alerts():
                if now - alert["created_at"] > GIVE_UP_AFTER:
                    self.store.mark_alert(alert["id"], sent=False, attempts=alert["attempts"],
                                          error="expired before it could be sent", give_up=True)
                    continue
                if self._next_send.get(alert["id"], 0) > now:
                    continue
                attempts = alert["attempts"] + 1
                try:
                    if not self.notifier.configured and not self._warned_unconfigured:
                        log.warning("Pushover is not configured; alerts are queued, not sent")
                        self._warned_unconfigured = True
                    self.notifier.send(alert["title"], alert["message"], alert["priority"],
                                       alert["sound"], alert["device"])
                except NotifyError as exc:
                    give_up = now - alert["created_at"] > GIVE_UP_AFTER
                    self.store.mark_alert(alert["id"], sent=False, attempts=attempts,
                                          error=str(exc), give_up=give_up)
                    self._next_send[alert["id"]] = now + min(30 * 2 ** (attempts - 1), 900)
                    log.warning("sending alert %r failed (attempt %d%s): %s", alert["title"],
                                attempts, ", giving up" if give_up else "", exc)
                else:
                    self.store.mark_alert(alert["id"], sent=True, attempts=attempts)
                    self._next_send.pop(alert["id"], None)
                    log.info("alert sent: %s", alert["title"])
        finally:
            self._send_lock.release()
