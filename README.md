# watchdog

A small, self-hosted monitor that sends alerts through [Pushover](https://pushover.net).
Pure Python standard library, no dependencies: one daemon, a SQLite file, and an admin TUI.

It does two kinds of check:

- **Push (dead-man's switch).** A cron job, worker or host pings the watchdog. You get an
  alert when a ping reports failure, when pings stop arriving, or when a job reports that
  it started but never finishes. This works for hosts behind firewalls, because the
  watchdog never has to reach them.
- **Pull (probe).** The watchdog checks the target itself on a schedule, by fetching a
  URL (for example a web server's index page, optionally checking for text on it) or by
  opening a TCP port.

Alerts are sent only when a check's state changes: one alert when it goes down, one when
it recovers. You can optionally add a single "still down" reminder. Checks can be paused
for maintenance. If a Pushover send fails, the alert waits in an outbox and is retried.

```
 watchdog  ·  3 checks  ·  1 down  ·  1 paused
 NAME                   TYPE  GROUP        STATUS          LAST SEEN   TARGET / REASON
 db-primary             tcp   ops          up (paused)     40s ago     db.example.com:5432
 nightly-backup         push  ops          up              30m ago     ping every 1d
 www-home               http  sites        down            20s ago     HTTP 503 (expected 200-399)

 a add  Enter view  e edit  p pause  r resume  d delete  / filter  t test alert  q quit
```

## Requirements

- Server: Linux with systemd, Python 3.9+ (with SQLite 3.24+), and a TLS reverse proxy
  (Apache or nginx) in front of it.
- Monitored hosts: `sh` and `curl`.

## Install (server)

```sh
git clone https://github.com/lucidbeaming/watchdog.git && cd watchdog
sudo deploy/install.sh
```

This creates a `watchdog` system user and installs:

- the code in `/opt/watchdog` and the `watchdog` command in `/usr/local/bin`;
- `/etc/watchdog/watchdog.ini` and `/etc/watchdog/pushover.env` (only if they don't
  already exist);
- the systemd unit `watchdogd.service`.

The SQLite database lives in `/var/lib/watchdog`.

Then:

1. Put your Pushover application token and user key in `/etc/watchdog/pushover.env`.
   Keys are case-insensitive:
   ```
   pushover_token=...
   pushover_user=...
   ```
2. Set `public_url` in `/etc/watchdog/watchdog.ini`.
3. Add the reverse-proxy snippet (`deploy/apache-snippet.conf` or
   `deploy/nginx-snippet.conf`) to your TLS virtual host, then reload the proxy.
4. Check that alerts reach your phone: `sudo watchdog test-alert`.
5. Start the daemon: `sudo systemctl enable --now watchdogd`.
6. Add checks: `sudo watchdog tui`.

To upgrade, pull the new code, re-run `sudo deploy/install.sh`, then run
`sudo systemctl restart watchdogd`. Existing config and data are kept.

## Admin TUI

`sudo watchdog tui` opens the list of checks, which refreshes every 2 seconds.

| Key | Action |
| --- | --- |
| `a` | add a check |
| `Enter` | details, ping URL and recent events |
| `e` | edit |
| `p` / `r` | pause for a duration (`30m`, `2h`, `1d`) / resume |
| `d` | delete (asks for confirmation) |
| `/` | filter by name, group, label or type |
| `t` | send a test notification |
| `n` | (in details) replace a push check's token |
| `q` | quit |

In the add/edit form:

- `↑`/`↓` or `Tab` move between fields.
- `←`/`→` or `Space` change a choice.
- `F2` or `Ctrl-S` saves; `Esc` cancels.

The fields shown depend on the type. Saving a push check generates its secret ping URL,
which you can select and copy from the details screen.

Changes take effect on the daemon's next tick (15 seconds by default), with no restart.
Run as root, the `watchdog` command switches to the `watchdog` user so the database
stays owned by the service.

## Check types and fields

| Type | What it watches | Key fields |
| --- | --- | --- |
| `push` | pings arrive every *interval* | interval, grace |
| `http` | `GET` *URL* returns an expected status | URL, expect status (`200-399`, `200,301`), expect text, timeout |
| `tcp` | a connection to *host:port* succeeds | host, port, timeout |

All check types also have:

- **label**: shown in alerts;
- **group**: for filtering and pausing together;
- **Pushover priority**: `-2` to `2`. Priority `2` repeats until acknowledged;
- **sound** and **device**;
- **remind after**;
- **enabled**.

For `http` and `tcp` checks, **failures before alert** sets how many probes must fail in
a row before the check counts as down.

A push check goes down when:

- the last ping was a failure (`/fail`, or a nonzero exit code);
- no ping has arrived for *interval + grace*;
- a `/start` ping has no matching `/ok` or `/fail` within *grace* (or within *interval*,
  if grace is 0).

## Ping API

Monitored hosts call these through the proxy, at `https://<your-domain>/watchdog/...`.
Both `GET` and `POST` work. A `POST` body (the last 2 KB is kept) becomes the event
message and appears in the alert.

| Path | Meaning |
| --- | --- |
| `/ping/<token>` or `/ping/<token>/ok` | success |
| `/ping/<token>/fail` | failure |
| `/ping/<token>/start` | job started |
| `/ping/<token>/<exit-code>` | `0` is success, anything else is failure |
| `/healthz` | `200` if the scheduler is running, otherwise `503` |

An unknown token returns `404`.

## Client tools (monitored hosts)

Install them with `sudo deploy/install.sh --client`, or copy the files in `client/` to
`/usr/local/bin`.

- `wd-ping <url> [ok|fail|start|<code>] [message|-]` sends one ping. A message of `-`
  reads it from stdin. It exits 0 even if the watchdog is unreachable, so monitoring
  never breaks a job; set `WD_STRICT=1` to change that.
- `wd-run <url> -- <command...>` wraps any job. It sends `start`, runs the command, then
  sends the exit code with the last 20 lines of output. The output is still printed for
  cron mail, and the exit code is passed through.
- `wd-probes.sh` is sourced by host self-check scripts. It provides `probe_systemd`,
  `probe_tcp`, `probe_http`, `probe_disk`, `probe_mem`, `probe_process` and `probe_cmd`.
  `wd_report <url>` then sends `ok`, or `fail` with one line per problem. See
  `examples/host-self-check.sh`.
- `wd-watch-watchdog <healthz-url> [env-file]` runs from cron on a *different* host. It
  sends Pushover alerts directly when the watchdog itself stops responding, and again
  when it recovers.

See `examples/crontab.example` for typical cron lines.

## Command line

```
watchdog serve                 run the daemon (used by systemd)
watchdog tui                   admin TUI
watchdog status [--group G]    table of checks; exits 1 if any is down
watchdog show NAME             details, ping URL, recent events
watchdog pause NAME|group:G 2h
watchdog resume NAME|group:G
watchdog test-alert [MESSAGE]
watchdog new-token
watchdog init-db
```

The config file is found from, in order: `-c FILE`, `$WATCHDOG_CONFIG`,
`./watchdog.ini`, then `/etc/watchdog/watchdog.ini`.
See `deploy/watchdog.example.ini` for every setting.

## Development

```sh
cp deploy/watchdog.example.ini watchdog.ini   # gitignored
# set: listen = 127.0.0.1:8787, public_url = http://127.0.0.1:8787,
#      [database] path = watchdog.db, [pushover] env_file = .env
bin/watchdog serve          # in one terminal
bin/watchdog tui            # in another
python3 -m unittest discover -s tests -t .
```

The tests cover:

- engine logic, against a fake clock;
- the HTTP API;
- the Pushover client, against a stub server;
- the shell clients, against a real daemon;
- the TUI, through a pseudo-terminal.

`.env`, `*.ini`, databases and `local/` are gitignored. Keep real hostnames and tokens
out of tracked files.

## License

MIT. See [LICENSE](LICENSE).
