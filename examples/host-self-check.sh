#!/bin/sh
# Example host self-check. Install the client tools (deploy/install.sh --client),
# copy this to /usr/local/sbin/host-self-check, edit the probes, and run it
# from cron (see crontab.example). Create a push check in the TUI and paste
# its ping URL below, or pass it as the first argument.
. /usr/local/bin/wd-probes.sh

PING_URL=${1:-https://watchdog.example.com/watchdog/ping/REPLACE_WITH_TOKEN}

probe_systemd nginx
probe_systemd cron
probe_disk / 90
probe_mem 10
probe_http http://127.0.0.1/ 200-399
probe_tcp 127.0.0.1 5432
probe_cmd "backup directory updated in the last day" -- \
  sh -c 'find /var/backups -maxdepth 1 -mtime -1 | grep -q .'

wd_report "$PING_URL"
