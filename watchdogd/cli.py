"""`watchdog` command line: daemon, admin TUI and quick admin commands."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time

from . import __version__, checks, config
from .db import Store, StoreError
from .notify import NotifyError, Pushover

log = logging.getLogger("watchdogd")


def drop_privileges(user: str):
    """When run as root, switch to the service user so SQLite files stay owned by it."""
    if os.name != "posix" or os.geteuid() != 0 or not user:
        return
    import pwd
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        return
    os.initgroups(pw.pw_name, pw.pw_gid)
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    os.environ["HOME"] = pw.pw_dir


def open_store(cfg) -> Store:
    store = Store(cfg.db_path)
    store.init_schema()
    return store


# -- commands ---------------------------------------------------------------

def cmd_serve(cfg, args):
    from .engine import Engine
    from .server import make_server

    store = open_store(cfg)
    notifier = Pushover.from_config(cfg)
    if not notifier.configured:
        log.warning("pushover_token/pushover_user not set: alerts will queue but not send")
    engine = Engine(store, notifier, cfg)
    httpd = make_server(cfg.listen_host, cfg.listen_port, engine)
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    log.info("watchdogd %s listening on %s:%d, database %s", __version__,
             cfg.listen_host, cfg.listen_port, cfg.db_path)

    stop = threading.Event()

    def on_signal(signum, _frame):
        log.info("received signal %d, stopping", signum)
        stop.set()
        engine.wake.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    while not stop.is_set():
        try:
            engine.tick()
        except Exception:
            log.exception("scheduler tick failed")
        engine.wake.wait(cfg.tick)
        engine.wake.clear()

    httpd.shutdown()
    engine.shutdown()
    store.close()
    return 0


def _status_rows(store, now, group=None):
    states = store.all_states()
    rows = []
    for c in store.list_checks():
        if group and c["group_name"] != group:
            continue
        s = states.get(c["id"]) or {}
        rows.append((c, s, display_status(c, s, now)))
    return rows


def display_status(check, state, now) -> str:
    if not check["enabled"]:
        return "disabled"
    status = (state or {}).get("status") or "new"
    if (check.get("paused_until") or 0) > now:
        return f"{status} (paused)"
    return status


def mute_text(until, now) -> str:
    """'' when notifications are on, else e.g. 'for 1h 59m' or 'until unmuted'."""
    if until is None or until <= now:
        return ""
    if until == float("inf"):
        return "until unmuted"
    return f"for {checks.fmt_duration(until - now)}"


def cmd_status(cfg, args):
    store = open_store(cfg)
    now = time.time()
    mute = mute_text(store.get_mute(), now)
    if mute:
        print(f"notifications muted {mute} (checks still running; `watchdog unmute`)\n")
    rows = _status_rows(store, now, args.group)
    if not rows:
        print("no checks yet: add some with `watchdog tui`")
        return 0
    headers = ("NAME", "TYPE", "GROUP", "STATUS", "LAST SEEN", "SINCE", "REASON")
    table = [headers]
    for c, s, status in rows:
        last = s.get("last_seen")
        table.append((
            c["name"], c["kind"], c["group_name"] or "-", status,
            f"{checks.fmt_duration(now - last)} ago" if last else "never",
            checks.fmt_duration(now - s["since"]) if s.get("since") else "-",
            (s.get("reason") or "").splitlines()[0][:60] if s.get("reason") else "",
        ))
    widths = [max(len(str(r[i])) for r in table) for i in range(len(headers))]
    for r in table:
        print("  ".join(str(v).ljust(w) for v, w in zip(r, widths)).rstrip())
    return 1 if any(s.get("status") == "down" for _, s, _ in rows) else 0


def cmd_show(cfg, args):
    store = open_store(cfg)
    c = store.get_check_by_name(args.name)
    if not c:
        print(f"no such check: {args.name}", file=sys.stderr)
        return 2
    for line in describe_check(cfg, c):
        print(line)
    print("\nrecent events:")
    for e in store.recent_events(c["id"], 20):
        print(f"  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e['ts']))}  "
              f"{e['kind']:<5} {'ok ' if e['ok'] else 'ERR'}  "
              f"{(e['message'] or '').splitlines()[0][:80] if e['message'] else ''}")
    return 0


def describe_check(cfg, c) -> list:
    lines = [f"{c['name']} ({c['kind']})"]
    if c["label"]:
        lines.append(f"  label:    {c['label']}")
    if c["group_name"]:
        lines.append(f"  group:    {c['group_name']}")
    if c["kind"] == "push":
        url = cfg.ping_url(c["token"])
        lines += [f"  ping URL: {url}",
                  f"  every {checks.fmt_duration(c['interval_s'])}, "
                  f"grace {checks.fmt_duration(c['grace_s'] or 0)}",
                  f"  cron:     wd-run {url} -- /path/to/job"]
    else:
        lines.append(f"  target:   {checks.describe_target(c)}")
        lines.append(f"  every {checks.fmt_duration(c['interval_s'])}, timeout "
                     f"{c['timeout_s']}s, alert after {c['failures_before_alert']} failure(s)")
    return lines


def _select(store, target):
    if target.startswith("group:"):
        group = target[6:]
        found = [c for c in store.list_checks() if c["group_name"] == group]
    else:
        c = store.get_check_by_name(target)
        found = [c] if c else []
    if not found:
        raise SystemExit(f"no check or group matches {target!r}")
    return found


def cmd_pause(cfg, args):
    store = open_store(cfg)
    try:
        seconds = checks.parse_duration(args.duration)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    found = _select(store, args.target)
    store.set_paused([c["id"] for c in found], time.time() + seconds)
    print(f"paused {', '.join(c['name'] for c in found)} for {checks.fmt_duration(seconds)}")
    return 0


def cmd_resume(cfg, args):
    store = open_store(cfg)
    found = _select(store, args.target)
    store.set_paused([c["id"] for c in found], None)
    print(f"resumed {', '.join(c['name'] for c in found)}")
    return 0


def cmd_mute(cfg, args):
    store = open_store(cfg)
    if args.duration:
        try:
            seconds = checks.parse_duration(args.duration)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        store.set_mute(time.time() + seconds)
        print(f"all notifications muted for {checks.fmt_duration(seconds)}; checks keep running")
    else:
        store.set_mute(float("inf"))
        print("all notifications muted until `watchdog unmute`; checks keep running")
    return 0


def cmd_unmute(cfg, args):
    open_store(cfg).set_mute(None)
    print("notifications on")
    return 0


def cmd_test_alert(cfg, args):
    try:
        Pushover.from_config(cfg).send("watchdog test", args.message, priority=args.priority)
    except NotifyError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    print("sent")
    return 0


def cmd_new_token(cfg, args):
    print(checks.new_token())
    return 0


def cmd_init_db(cfg, args):
    open_store(cfg)
    print(f"database ready: {cfg.db_path}")
    return 0


def cmd_tui(cfg, args):
    from .tui import run
    return run(cfg, open_store(cfg))


def build_parser():
    p = argparse.ArgumentParser(prog="watchdog", description=__doc__)
    p.add_argument("-c", "--config", help="config file (default: $WATCHDOG_CONFIG, "
                   "./watchdog.ini, /etc/watchdog/watchdog.ini)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    sub.add_parser("serve", help="run the daemon (HTTP receiver + scheduler)").set_defaults(
        func=cmd_serve)
    sub.add_parser("tui", help="interactive admin: add, edit, pause and inspect checks"
                   ).set_defaults(func=cmd_tui)
    s = sub.add_parser("status", help="table of all checks (exit 1 if any is down)")
    s.add_argument("--group", help="only this group")
    s.set_defaults(func=cmd_status)
    s = sub.add_parser("show", help="details, ping URL and recent events for one check")
    s.add_argument("name")
    s.set_defaults(func=cmd_show)
    s = sub.add_parser("pause", help="silence alerts, e.g. `pause web1 2h` or `pause group:db 1d`")
    s.add_argument("target", help="check name or group:NAME")
    s.add_argument("duration", help="e.g. 30m, 2h, 1d")
    s.set_defaults(func=cmd_pause)
    s = sub.add_parser("resume", help="end a pause")
    s.add_argument("target", help="check name or group:NAME")
    s.set_defaults(func=cmd_resume)
    s = sub.add_parser("mute", help="stop all notifications (checks keep running), "
                       "e.g. `mute 2h`; no duration = until unmuted")
    s.add_argument("duration", nargs="?", help="e.g. 30m, 2h, 1d")
    s.set_defaults(func=cmd_mute)
    sub.add_parser("unmute", help="turn notifications back on").set_defaults(func=cmd_unmute)
    s = sub.add_parser("test-alert", help="send one Pushover message")
    s.add_argument("message", nargs="?", default="Test alert from watchdog.")
    s.add_argument("--priority", type=int, default=0, choices=checks.PRIORITIES)
    s.set_defaults(func=cmd_test_alert)
    sub.add_parser("new-token", help="print a random ping token").set_defaults(func=cmd_new_token)
    sub.add_parser("init-db", help="create the database tables").set_defaults(func=cmd_init_db)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.command == "new-token":
        return cmd_new_token(None, args)
    try:
        cfg = config.load(args.config)
    except config.ConfigError as exc:
        print(f"watchdog: {exc}", file=sys.stderr)
        return 2
    drop_privileges(cfg.run_as)
    try:
        return args.func(cfg, args)
    except StoreError as exc:
        print(f"watchdog: {exc}", file=sys.stderr)
        return 2
