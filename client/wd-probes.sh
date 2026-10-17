# wd-probes.sh: helpers for host self-check scripts (POSIX sh). Source it:
#
#   . /usr/local/bin/wd-probes.sh
#   probe_systemd nginx
#   probe_disk / 90
#   wd_report https://watchdog.example.com/ping/<token>
#
# Each probe_* appends a line to WD_FAILURES when it fails. wd_report sends
# "ok" if nothing failed, otherwise "fail" with one line per problem.

WD_FAILURES=""
WD_TIMEOUT=${WD_TIMEOUT:-10}

_wd_fail() {
  if [ -z "$WD_FAILURES" ]; then
    WD_FAILURES="$*"
  else
    WD_FAILURES="$WD_FAILURES
$*"
  fi
}

# probe_systemd <unit>: the unit is active.
probe_systemd() {
  systemctl is-active --quiet "$1" || _wd_fail "systemd: $1 is $(systemctl is-active "$1" 2>/dev/null || echo unknown)"
}

# probe_tcp <host> <port>: a TCP connection succeeds.
probe_tcp() {
  if command -v nc >/dev/null 2>&1; then
    nc -z -w "$WD_TIMEOUT" "$1" "$2" >/dev/null 2>&1 && return 0
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import socket,sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=float(sys.argv[3])).close()' \
      "$1" "$2" "$WD_TIMEOUT" 2>/dev/null && return 0
  else
    _wd_fail "tcp: neither nc nor python3 available to check $1:$2"
    return 1
  fi
  _wd_fail "tcp: cannot connect to $1:$2"
}

# probe_http <url> [expected]: status matches, e.g. 200 or 200-399 (default).
probe_http() {
  _want=${2:-200-399}
  _code=$(curl -s -o /dev/null -w '%{http_code}' -m "$WD_TIMEOUT" "$1" 2>/dev/null)
  _lo=${_want%-*}
  _hi=${_want#*-}
  if [ -z "$_code" ] || [ "$_code" = 000 ]; then
    _wd_fail "http: $1 unreachable"
  elif [ "$_code" -lt "$_lo" ] || [ "$_code" -gt "$_hi" ]; then
    _wd_fail "http: $1 returned $_code (expected $_want)"
  fi
}

# probe_disk <mount> <max-percent>: usage at or below the limit.
probe_disk() {
  _used=$(df -P "$1" 2>/dev/null | awk 'NR==2 { sub("%", "", $5); print $5 }')
  if [ -z "$_used" ]; then
    _wd_fail "disk: cannot read usage of $1"
  elif [ "$_used" -gt "$2" ]; then
    _wd_fail "disk: $1 is ${_used}% full (limit $2%)"
  fi
}

# probe_mem <min-percent-available>: Linux only (MemAvailable).
probe_mem() {
  [ -r /proc/meminfo ] || return 0
  _avail=$(awk '/^MemTotal:/ { t=$2 } /^MemAvailable:/ { a=$2 } END { if (t) printf "%d", a*100/t }' /proc/meminfo)
  [ -n "$_avail" ] && [ "$_avail" -lt "$1" ] && _wd_fail "memory: only ${_avail}% available (minimum $1%)"
  return 0
}

# probe_process <name>: a process with exactly this name is running.
probe_process() {
  pgrep -x "$1" >/dev/null 2>&1 || _wd_fail "process: $1 is not running"
}

# probe_cmd <description> -- <command...>: the command exits 0.
probe_cmd() {
  _desc=$1
  shift
  [ "${1:-}" = "--" ] && shift
  "$@" >/dev/null 2>&1 || _wd_fail "$_desc"
}

# wd_report <ping-url>: send the result.
wd_report() {
  _here=$(dirname "$0")
  _ping=wd-ping
  [ -x "$_here/wd-ping" ] && _ping="$_here/wd-ping"
  if [ -z "$WD_FAILURES" ]; then
    "$_ping" "$1" ok
  else
    printf '%s\n' "$WD_FAILURES" | "$_ping" "$1" fail -
  fi
}
