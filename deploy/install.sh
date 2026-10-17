#!/bin/sh
# Install watchdog on a Linux host with systemd. Run as root from a checkout:
#
#   sudo deploy/install.sh            # server: daemon, admin CLI/TUI and client tools
#   sudo deploy/install.sh --client   # monitored host: client tools only
#
# Re-running upgrades the code and keeps existing config and data.
set -eu

PREFIX=/opt/watchdog
CONF=/etc/watchdog
DATA=/var/lib/watchdog
BIN=/usr/local/bin
UNIT=/etc/systemd/system/watchdogd.service
SVC_USER=watchdog

die() { echo "install: $*" >&2; exit 1; }
src=$(cd "$(dirname "$0")/.." && pwd)
mode=${1:-server}

[ "$(id -u)" = 0 ] || die "run as root"
[ -f "$src/watchdogd/cli.py" ] || die "run from a watchdog checkout"

for f in wd-ping wd-run wd-watch-watchdog; do
  install -m 755 "$src/client/$f" "$BIN/$f"
done
install -m 644 "$src/client/wd-probes.sh" "$BIN/wd-probes.sh"
echo "installed client tools to $BIN"
command -v curl >/dev/null || echo "warning: curl is required by the client tools" >&2

[ "$mode" = "--client" ] && exit 0
[ "$mode" = "server" ] || die "unknown option: $mode"

command -v python3 >/dev/null || die "python3 not found"
python3 - <<'EOF' || die "need Python >= 3.9 with sqlite3 >= 3.24"
import sqlite3, sys
ok = sys.version_info >= (3, 9) and sqlite3.sqlite_version_info >= (3, 24)
print(f"python {sys.version.split()[0]}, sqlite {sqlite3.sqlite_version}")
sys.exit(0 if ok else 1)
EOF

if ! id "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
  echo "created system user $SVC_USER"
fi

install -d -m 755 "$PREFIX"
rm -rf "$PREFIX/watchdogd"
cp -R "$src/watchdogd" "$PREFIX/watchdogd"
find "$PREFIX/watchdogd" -name __pycache__ -prune -exec rm -rf {} +
chmod -R u=rwX,go=rX "$PREFIX/watchdogd"

cat >"$BIN/watchdog" <<EOF
#!/bin/sh
PYTHONPATH="$PREFIX" exec python3 -m watchdogd "\$@"
EOF
chmod 755 "$BIN/watchdog"
echo "installed $PREFIX/watchdogd and $BIN/watchdog"

install -d -m 750 -o root -g "$SVC_USER" "$CONF"
if [ ! -e "$CONF/watchdog.ini" ]; then
  install -m 640 -o root -g "$SVC_USER" "$src/deploy/watchdog.example.ini" "$CONF/watchdog.ini"
  echo "created $CONF/watchdog.ini (edit public_url)"
fi
if [ ! -e "$CONF/pushover.env" ]; then
  install -m 600 -o root -g root "$src/deploy/pushover.example.env" "$CONF/pushover.env"
  echo "created $CONF/pushover.env (add your Pushover keys)"
fi
install -d -m 750 -o "$SVC_USER" -g "$SVC_USER" "$DATA"

install -m 644 "$src/deploy/watchdogd.service" "$UNIT"
systemctl daemon-reload
echo "installed $UNIT"

cat <<EOF

Next steps:
  1. Edit $CONF/pushover.env and $CONF/watchdog.ini (public_url).
  2. Add the reverse-proxy snippet (deploy/apache-snippet.conf or nginx-snippet.conf).
  3. sudo watchdog test-alert
  4. sudo systemctl enable --now watchdogd   (or: systemctl restart watchdogd after an upgrade)
  5. sudo watchdog tui                        (add checks)
EOF
