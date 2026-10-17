"""Admin TUI (stdlib curses): list, add, edit, pause and delete checks.

The TUI writes check definitions to the database; the running daemon picks
them up on its next scheduler tick, so no restart or reload is needed.
"""

from __future__ import annotations

import curses
import locale
import os
import time

from . import checks
from .checks import FIELD_BY_NAME, KIND_HELP, blank_check, fields_for, new_token, validate
from .cli import describe_check, display_status, mute_text
from .db import StoreError
from .notify import NotifyError, Pushover

ESC = "\x1b"
ENTER = ("\n", "\r", curses.KEY_ENTER)
BACKSPACE = (curses.KEY_BACKSPACE, "\x7f", "\b")
REFRESH_MS = 2000


def ctrl(c: str) -> str:
    return chr(ord(c) & 0x1F)


def run(cfg, store) -> int:
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    try:
        return curses.wrapper(lambda scr: App(scr, cfg, store).main()) or 0
    finally:
        store.close()


def edit_line(text: str, pos: int, key, digits_only=False):
    """Apply one key to a single-line text buffer. Returns (text, pos, handled)."""
    if key in (curses.KEY_LEFT,):
        return text, max(0, pos - 1), True
    if key in (curses.KEY_RIGHT,):
        return text, min(len(text), pos + 1), True
    if key in (curses.KEY_HOME, ctrl("a")):
        return text, 0, True
    if key in (curses.KEY_END, ctrl("e")):
        return text, len(text), True
    if key == ctrl("u"):
        return "", 0, True
    if key in BACKSPACE:
        if pos > 0:
            return text[:pos - 1] + text[pos:], pos - 1, True
        return text, pos, True
    if key == curses.KEY_DC:
        return text[:pos] + text[pos + 1:], pos, True
    if isinstance(key, str) and len(key) == 1 and key.isprintable():
        if digits_only and not key.isdigit():
            return text, pos, True
        return text[:pos] + key + text[pos:], pos + 1, True
    return text, pos, False


class App:
    def __init__(self, scr, cfg, store):
        self.scr, self.cfg, self.store = scr, cfg, store
        self.sel = 0
        self.top = 0
        self.filter = ""
        self.msg = ""
        self.msg_error = False
        self.msg_until = 0.0
        curses.raw()
        scr.keypad(True)
        self._cursor(False)
        self.colors = {}
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            for i, (name, fg) in enumerate((("up", curses.COLOR_GREEN),
                                            ("down", curses.COLOR_RED),
                                            ("warn", curses.COLOR_YELLOW),
                                            ("accent", curses.COLOR_CYAN)), start=1):
                curses.init_pair(i, fg, bg)
                self.colors[name] = curses.color_pair(i)

    # -- drawing helpers ---------------------------------------------------

    def c(self, name) -> int:
        return self.colors.get(name, 0)

    def status_attr(self, status: str) -> int:
        if status.startswith("down"):
            return self.c("down") | curses.A_BOLD
        if status.startswith("up") and "paused" not in status:
            return self.c("up")
        if status == "disabled":
            return curses.A_DIM
        return self.c("warn")

    def _cursor(self, visible: bool):
        try:
            curses.curs_set(1 if visible else 0)
        except curses.error:
            pass

    def put(self, y, x, text, attr=0, width=None):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        limit = w - x if width is None else min(width, w - x)
        text = str(text).replace("\n", " ")[:max(0, limit)]
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass  # writing the bottom-right cell raises; harmless

    def bar(self, y, text, attr=curses.A_REVERSE):
        _, w = self.scr.getmaxyx()
        self.put(y, 0, text.ljust(w), attr)

    def flash(self, msg, error=False, seconds=4.0):
        self.msg, self.msg_error, self.msg_until = msg, error, time.time() + seconds

    def draw_flash(self, y):
        if self.msg and time.time() < self.msg_until:
            self.put(y, 1, self.msg, self.c("down") | curses.A_BOLD if self.msg_error
                     else self.c("accent"))

    def getkey(self, timeout_ms=-1):
        self.scr.timeout(timeout_ms)
        try:
            return self.scr.get_wch()
        except curses.error:
            return None

    def prompt(self, label, initial="", digits_only=False):
        """Single-line input on the bottom rows. Returns text, or None on Esc."""
        text, pos = initial, len(initial)
        self._cursor(True)
        try:
            while True:
                h, w = self.scr.getmaxyx()
                self.bar(h - 2, "", 0)
                self.put(h - 2, 1, label, curses.A_BOLD)
                x = 2 + len(label)
                avail = max(5, w - x - 1)
                start = max(0, pos - avail + 1)
                self.put(h - 2, x, text[start:start + avail])
                self.bar(h - 1, " Enter accept   Esc cancel")
                self.scr.move(h - 2, min(w - 1, x + pos - start))
                key = self.getkey()
                if key is None or key == curses.KEY_RESIZE:
                    continue
                if key in ENTER:
                    return text.strip()
                if key in (ESC, ctrl("c")):
                    return None
                text, pos, _ = edit_line(text, pos, key, digits_only)
        finally:
            self._cursor(False)

    def confirm(self, question) -> bool:
        h, _ = self.scr.getmaxyx()
        self.bar(h - 2, "", 0)
        self.put(h - 2, 1, f"{question} [y/N]", self.c("warn") | curses.A_BOLD)
        self.bar(h - 1, " y confirm   any other key cancel")
        key = self.getkey()
        return key in ("y", "Y")

    # -- list screen -------------------------------------------------------

    def load_rows(self):
        now = time.time()
        states = self.store.all_states()
        rows = []
        needle = self.filter.lower()
        for ch in self.store.list_checks():
            if needle and needle not in " ".join(
                    (ch["name"], ch["group_name"], ch["label"], ch["kind"])).lower():
                continue
            st = states.get(ch["id"]) or {}
            rows.append((ch, st, display_status(ch, st, now)))
        return rows

    def draw_list(self, rows):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        now = time.time()
        down = sum(1 for _, _, s in rows if s.startswith("down"))
        paused = sum(1 for _, _, s in rows if "paused" in s)
        head = f" watchdog  ·  {len(rows)} check{'s' * (len(rows) != 1)}"
        if self.filter:
            head += f" matching '{self.filter}'"
        head += f"  ·  {down} down" + (f"  ·  {paused} paused" if paused else "")
        mute = mute_text(self.store.get_mute(), now)
        if mute:
            head += f"  ·  NOTIFICATIONS MUTED {mute}"
        color = self.c("down") if down else (self.c("warn") if mute else 0)
        self.bar(0, head, curses.A_REVERSE | color | (curses.A_BOLD if mute else 0))

        cols = (("NAME", 22), ("TYPE", 5), ("GROUP", 12), ("STATUS", 15), ("LAST SEEN", 11))
        x = 1
        for title, width in cols:
            self.put(1, x, title, curses.A_BOLD)
            x += width + 1
        self.put(1, x, "TARGET / REASON", curses.A_BOLD)

        body_h = max(1, h - 4)
        if self.sel >= len(rows):
            self.sel = max(0, len(rows) - 1)
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + body_h:
            self.top = self.sel - body_h + 1

        if not rows:
            self.put(3, 2, "No checks yet. Press  a  to add one." if not self.filter
                     else "Nothing matches the filter. Press  /  to change it.", self.c("warn"))
        for i, (ch, st, status) in enumerate(rows[self.top:self.top + body_h]):
            y = 2 + i
            selected = self.top + i == self.sel
            base = curses.A_REVERSE if selected else 0
            if selected:
                self.bar(y, "", curses.A_REVERSE)
            last = st.get("last_seen")
            seen = f"{checks.fmt_duration(now - last)} ago" if last else "never"
            detail = st.get("reason") if status.startswith("down") else checks.describe_target(ch)
            values = (ch["name"], ch["kind"], ch["group_name"] or "-", status, seen)
            x = 1
            for (title, width), value in zip(cols, values):
                attr = base | (self.status_attr(status) if title == "STATUS" else 0)
                self.put(y, x, str(value), attr, width)
                x += width + 1
            self.put(y, x, (detail or "").splitlines()[0] if detail else "", base)

        self.draw_flash(h - 2)
        self.bar(h - 1, " a add  Enter view  e edit  p pause  r resume  d delete  "
                        "/ filter  m mute all  t test alert  q quit")
        self.scr.noutrefresh()
        curses.doupdate()

    def main(self) -> int:
        while True:
            try:
                rows = self.load_rows()
            except StoreError as exc:
                self.flash(str(exc), error=True)
                rows = []
            self.draw_list(rows)
            key = self.getkey(REFRESH_MS)
            if key is None or key == curses.KEY_RESIZE:
                continue
            current = rows[self.sel][0] if rows else None
            if key in ("q", "Q", ctrl("c")):
                return 0
            if key in (curses.KEY_UP, "k"):
                self.sel = max(0, self.sel - 1)
            elif key in (curses.KEY_DOWN, "j"):
                self.sel = min(max(0, len(rows) - 1), self.sel + 1)
            elif key == curses.KEY_PPAGE:
                self.sel = max(0, self.sel - 10)
            elif key == curses.KEY_NPAGE:
                self.sel = min(max(0, len(rows) - 1), self.sel + 10)
            elif key in ("g", curses.KEY_HOME):
                self.sel = 0
            elif key in ("G", curses.KEY_END):
                self.sel = max(0, len(rows) - 1)
            elif key == "a":
                new_id = self.edit_form(None)
                if new_id:
                    self.detail(new_id)
            elif key == "/":
                text = self.prompt("Filter:", self.filter)
                if text is not None:
                    self.filter, self.sel = text, 0
            elif key == "t":
                self.test_alert()
            elif key == "m":
                self.toggle_mute()
            elif current is None:
                continue
            elif key in ENTER or key == "v":
                self.detail(current["id"])
            elif key == "e":
                self.edit_form(current)
            elif key == "p":
                self.pause(current)
            elif key == "r":
                self.store.set_paused([current["id"]], None)
                self.flash(f"resumed {current['name']}")
            elif key == "d":
                if self.confirm(f"Delete {current['name']} and its history?"):
                    self.store.delete_check(current["id"])
                    self.flash(f"deleted {current['name']}")

    # -- actions -----------------------------------------------------------

    def pause(self, check):
        text = self.prompt(f"Pause {check['name']} for (e.g. 30m, 2h, 1d):", "1h")
        if not text:
            return
        try:
            seconds = checks.parse_duration(text)
        except ValueError as exc:
            self.flash(str(exc), error=True)
            return
        self.store.set_paused([check["id"]], time.time() + seconds)
        self.flash(f"paused {check['name']} for {checks.fmt_duration(seconds)}")

    def toggle_mute(self):
        now = time.time()
        mute = mute_text(self.store.get_mute(), now)
        if mute:
            if self.confirm(f"Notifications are muted {mute}. Unmute now?"):
                self.store.set_mute(None)
                self.flash("notifications on; checks still down will alert on the next tick")
            return
        text = self.prompt("Mute ALL notifications for (e.g. 2h, 1d; empty = until unmuted):")
        if text is None:
            return
        if not text:
            self.store.set_mute(float("inf"))
            self.flash("all notifications muted until you unmute (m); checks keep running",
                       seconds=6)
            return
        try:
            seconds = checks.parse_duration(text)
        except ValueError as exc:
            self.flash(str(exc), error=True)
            return
        self.store.set_mute(now + seconds)
        self.flash(f"all notifications muted for {checks.fmt_duration(seconds)}; "
                   "checks keep running", seconds=6)

    def test_alert(self):
        if not self.confirm("Send a test notification through Pushover?"):
            return
        try:
            Pushover.from_config(self.cfg).send("watchdog test", "Test alert from watchdog TUI.")
            self.flash("test alert sent")
        except NotifyError as exc:
            self.flash(f"test alert failed: {exc}", error=True, seconds=8)

    # -- detail screen -----------------------------------------------------

    def detail(self, check_id):
        while True:
            ch = self.store.get_check(check_id)
            if ch is None:
                return
            st = self.store.get_state(check_id) or {}
            now = time.time()
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            status = display_status(ch, st, now)
            self.bar(0, f" {ch['name']}  ·  {status}",
                     curses.A_REVERSE | (self.c("down") if status.startswith("down") else 0))
            y = 2
            for line in describe_check(self.cfg, ch):
                self.put(y, 1, line, curses.A_BOLD if y == 2 else 0)
                y += 1
            y += 1
            facts = [("status", status, self.status_attr(status))]
            if st.get("since"):
                facts.append(("since", f"{checks.fmt_duration(now - st['since'])} ago", 0))
            if st.get("reason"):
                facts.append(("reason", st["reason"].splitlines()[0], 0))
            facts.append(("last seen", f"{checks.fmt_duration(now - st['last_seen'])} ago"
                          if st.get("last_seen") else "never", 0))
            if (ch.get("paused_until") or 0) > now:
                facts.append(("paused", f"for another "
                              f"{checks.fmt_duration(ch['paused_until'] - now)}",
                              self.c("warn")))
            mute = mute_text(self.store.get_mute(), now)
            if mute:
                facts.append(("alerts", f"all notifications muted {mute}", self.c("warn")))
            if ch["kind"] != "push" and st.get("consecutive_failures"):
                facts.append(("failures", f"{st['consecutive_failures']} in a row", 0))
            for label, value, attr in facts:
                self.put(y, 3, f"{label:>10}  ")
                self.put(y, 15, value, attr)
                y += 1

            y += 1
            self.put(y, 1, "Recent events", curses.A_BOLD)
            y += 1
            events = self.store.recent_events(check_id, max(1, h - y - 3))
            if not events:
                self.put(y, 3, "none yet", curses.A_DIM)
            for ev in events:
                if y >= h - 2:
                    break
                stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(ev["ts"]))
                first = (ev["message"] or "").strip().splitlines()
                self.put(y, 3, stamp, curses.A_DIM)
                self.put(y, 19, f"{ev['kind']:<5}")
                self.put(y, 25, "ok " if ev["ok"] else "ERR",
                         self.c("up") if ev["ok"] else self.c("down"))
                self.put(y, 29, first[0] if first else "")
                y += 1

            self.draw_flash(h - 2)
            keys = " e edit  p pause  r resume  d delete"
            if ch["kind"] == "push":
                keys += "  n new token"
            self.bar(h - 1, keys + "  Esc back")
            self.scr.noutrefresh()
            curses.doupdate()

            key = self.getkey(REFRESH_MS)
            if key is None or key == curses.KEY_RESIZE:
                continue
            if key in (ESC, "q", curses.KEY_LEFT, ctrl("c")):
                return
            if key == "e":
                self.edit_form(ch)
            elif key == "p":
                self.pause(ch)
            elif key == "r":
                self.store.set_paused([check_id], None)
                self.flash(f"resumed {ch['name']}")
            elif key == "d":
                if self.confirm(f"Delete {ch['name']} and its history?"):
                    self.store.delete_check(check_id)
                    self.flash(f"deleted {ch['name']}")
                    return
            elif key == "n" and ch["kind"] == "push":
                if self.confirm("Replace the ping token? Clients using the old URL will 404."):
                    self.store.update_check(check_id, {"token": new_token()})
                    self.flash("new token set; update the ping URL on the monitored host",
                               seconds=8)

    # -- add / edit form ---------------------------------------------------

    def edit_form(self, check):
        """Returns the check id when saved, else None."""
        is_new = check is None
        values = blank_check(self.cfg.defaults) if is_new else dict(check)
        buf = {}
        for f in checks.FIELDS:
            v = values.get(f.name)
            if f.type == "bool":
                buf[f.name] = "yes" if v else "no"
            else:
                buf[f.name] = "" if v is None else str(v)
        if not is_new:
            # Show defaults for fields that don't apply to the current kind, in case it changes.
            for k, v in blank_check(self.cfg.defaults).items():
                if not buf.get(k) and v not in (None, ""):
                    buf[k] = "yes" if FIELD_BY_NAME[k].type == "bool" and v else str(v)
        original = dict(buf)
        self._form_kind = None
        errors: dict = {}
        idx, pos = 0, len(buf["name"])
        self._cursor(True)
        try:
            while True:
                flds = fields_for(buf["kind"]) + ["save", "cancel"]
                idx = min(idx, len(flds) - 1)
                cur = flds[idx]
                self.draw_form(is_new, check, buf, flds, idx, pos, errors)
                key = self.getkey()
                if key is None or key == curses.KEY_RESIZE:
                    continue

                if key in (ESC, ctrl("c")) or (key in ENTER and cur == "cancel"):
                    if buf == original or self.confirm("Discard changes?"):
                        return None
                    continue
                if key in (curses.KEY_F2, ctrl("s")) or (key in ENTER and cur == "save"):
                    saved = self.save_form(is_new, check, buf, errors)
                    if saved:
                        return saved
                    first = next((i for i, f in enumerate(flds)
                                  if not isinstance(f, str) and f.name in errors), idx)
                    idx = first
                    pos = len(buf[flds[idx].name]) if not isinstance(flds[idx], str) else 0
                    continue

                move = 0
                if key in (curses.KEY_UP, curses.KEY_BTAB):
                    move = -1
                elif key in (curses.KEY_DOWN, "\t") or key in ENTER:
                    move = 1
                if move:
                    idx = (idx + move) % len(flds)
                    nxt = flds[idx]
                    pos = len(buf[nxt.name]) if not isinstance(nxt, str) else 0
                    continue

                if isinstance(cur, str):  # Save / Cancel buttons
                    if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
                        idx = len(flds) - 2 if cur == "cancel" else len(flds) - 1
                    continue

                if cur.type in ("choice", "bool"):
                    options = [str(o) for o in cur.choices] if cur.type == "choice" \
                        else ["yes", "no"]
                    step = {curses.KEY_LEFT: -1, curses.KEY_RIGHT: 1, " ": 1}.get(key)
                    if step:
                        i = options.index(buf[cur.name]) if buf[cur.name] in options else 0
                        buf[cur.name] = options[(i + step) % len(options)]
                        errors.pop(cur.name, None)
                    continue

                text, pos, handled = edit_line(buf[cur.name], pos, key,
                                               digits_only=cur.type == "int")
                if handled:
                    buf[cur.name] = text
                    errors.pop(cur.name, None)
        finally:
            self._cursor(False)

    def save_form(self, is_new, check, buf, errors):
        clean, errs = validate(buf)
        errors.clear()
        errors.update(errs)
        if errs:
            self.flash(f"{len(errs)} field(s) need attention", error=True)
            return None
        try:
            if is_new:
                if clean["kind"] == "push":
                    clean["token"] = new_token()
                check_id = self.store.add_check(clean)
                self.flash(f"added {clean['name']}")
                return check_id
            check_id = check["id"]
            if clean["kind"] == "push":
                clean["token"] = check.get("token") or new_token()
            else:
                clean["token"] = None
            self.store.update_check(check_id, clean)
            if clean["kind"] != check["kind"] or (clean["enabled"] and not check["enabled"]):
                self.store.reset_state(check_id)  # old status no longer meaningful
            self.flash(f"saved {clean['name']}")
            return check_id
        except StoreError as exc:
            errors["name"] = "already exists" if "already exists" in str(exc) else str(exc)
            self.flash(str(exc), error=True)
            return None

    def draw_form(self, is_new, check, buf, flds, idx, pos, errors):
        if getattr(self, "_form_kind", None) != buf["kind"]:
            # Rows were added/removed: repaint fully instead of letting curses scroll
            # regions around, which some terminals and multiplexers render badly.
            self._form_kind = buf["kind"]
            self.scr.clear()
        else:
            self.scr.erase()
        h, w = self.scr.getmaxyx()
        self.bar(0, " Add check" if is_new else f" Edit check  ·  {check['name']}")
        label_w = max(len(f.label) for f in checks.FIELDS) + 2
        x0 = label_w + 3
        val_w = max(10, w - x0 - 2)
        y = 2
        cursor_at = None
        for i, f in enumerate(flds):
            if isinstance(f, str):
                break
            selected = i == idx
            self.put(y, 1, f.label.rjust(label_w), curses.A_BOLD if selected else 0)
            if f.required:
                self.put(y, label_w + 1, "*", self.c("warn"))
            if f.type in ("choice", "bool"):
                options = [str(o) for o in f.choices] if f.type == "choice" else ["yes", "no"]
                x = x0
                for opt in options:
                    on = opt == buf[f.name]
                    attr = (curses.A_REVERSE if on else curses.A_DIM)
                    if on and selected:
                        attr |= curses.A_BOLD | self.c("accent")
                    self.put(y, x, f" {opt} ", attr)
                    x += len(opt) + 3
                if selected:
                    cursor_at = (y, x0)
            else:
                text = buf[f.name]
                start = max(0, pos - val_w + 1) if selected else 0
                shown = text[start:start + val_w]
                self.put(y, x0, shown.ljust(min(val_w, 40)) if selected else shown,
                         curses.A_UNDERLINE if selected else 0)
                if selected:
                    cursor_at = (y, x0 + pos - start)
            if f.name in errors:
                ex = x0 + min(val_w, max(len(buf[f.name]), 40 if selected else 0)) + 2
                self.put(y, ex, f"← {errors[f.name]}", self.c("down") | curses.A_BOLD)
            y += 1

        y += 1
        save_sel, cancel_sel = flds[idx] == "save", flds[idx] == "cancel"
        save_attr = (curses.A_REVERSE | self.c("up")) if save_sel else curses.A_BOLD
        self.put(y, x0, "[ Save ]", save_attr)
        self.put(y, x0 + 11, "[ Cancel ]", curses.A_REVERSE if cancel_sel else 0)
        if save_sel or cancel_sel:
            cursor_at = None
        y += 2
        if not is_new and check["kind"] == "push" and buf["kind"] == "push" and check["token"]:
            self.put(y, 1, "Ping URL".rjust(label_w), curses.A_DIM)
            self.put(y, x0, self.cfg.ping_url(check["token"]), curses.A_DIM)
            y += 1

        cur = flds[idx]
        if isinstance(cur, str):
            hint = "save this check" if cur == "save" else "discard changes"
        elif cur.name == "kind":
            hint = f"{buf['kind']}: {KIND_HELP.get(buf['kind'], '')}"
        else:
            hint = cur.help
        self.put(h - 3, 1, hint, self.c("accent"))
        self.draw_flash(h - 2)
        self.bar(h - 1, " ↑↓/Tab move  ←→/Space change choice  F2 or Ctrl-S save  Esc cancel")
        if cursor_at:
            self._cursor(True)
            try:
                self.scr.move(*cursor_at)
            except curses.error:
                pass
        else:
            self._cursor(False)
        self.scr.noutrefresh()
        curses.doupdate()
