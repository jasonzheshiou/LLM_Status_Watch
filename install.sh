#!/usr/bin/env bash
# Install / drive LLM_Status_Watch on the Raspberry Pi. Run it ON the Pi.
#
#   ./install.sh test            --explain: one poll, print the verdict, change nothing
#   ./install.sh test-notify     publish one real test notification (needs your phone subscribed)
#   ./install.sh dry-run [min]   foreground trial, logs decisions instead of pushing (default 10 min)
#   ./install.sh trial           background trial with the real config; log is the evidence
#   ./install.sh trial-stop      stop the background trial
#   ./install.sh install         copy files to ~/LLM_Status_Watch and enable the systemd service
#   ./install.sh status | logs | stop | restart | uninstall
#
# Nothing here touches the LLM server; the watcher only reads its /metrics over HTTP.
set -uo pipefail

SRC="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DST="$HOME/LLM_Status_Watch"
UNIT="LLM_Status_Watch"
SUDO="sudo -n"
PY=/usr/bin/python3

err() { printf 'install.sh: %s\n' "$1" >&2; exit 1; }

need_python() { [[ -x $PY ]] || err "python3 not at $PY"; }
do_notify_test() { need_python; "$PY" "$SRC/LLM_Status_Watch.py" --test-notify; }

case "${1:-help}" in
  test)
    need_python; exec "$PY" "$SRC/LLM_Status_Watch.py" --explain ;;

  test-notify)
    need_python; do_notify_test ;;

  dry-run)
    need_python
    mins="${2:-10}"                       # how long to watch
    idle="${TRIAL_IDLE_MINUTES:-1}"       # threshold used for the trial, short on purpose
    poll="${TRIAL_POLL:-15}"
    echo "== trial for ${mins} min: nothing is pushed to your phone, decisions are logged =="
    echo "== trial threshold: idle ${idle} min, poll ${poll} s (real values are in LLM_Status_Watch.conf) =="
    exec timeout $((mins * 60)) "$PY" "$SRC/LLM_Status_Watch.py" --dry-run \
         --idle-minutes "$idle" --poll-seconds "$poll" ;;

  install)
    need_python
    if [[ "$(readlink -f "$SRC")" != "$(readlink -f "$DST")" ]]; then
      install -d -m 755 "$DST" || err "cannot create $DST"
      install -m 755 "$SRC/LLM_Status_Watch.py"  "$DST/LLM_Status_Watch.py"  || err "copy failed"
      install -m 644 "$SRC/README.md"       "$DST/README.md"       || err "copy failed"
      install -m 600 "$SRC/LLM_Status_Watch.conf" "$DST/LLM_Status_Watch.conf" || err "copy failed"
      echo "installed into $DST"
    fi
    chmod 600 "$DST/LLM_Status_Watch.conf"
    $SUDO true 2>/dev/null || err "passwordless sudo unavailable; use the cron recipe in README.md"
    $SUDO install -m 644 "$SRC/$UNIT.service" "/etc/systemd/system/$UNIT.service" || err "unit copy failed"
    $SUDO systemctl daemon-reload            || err "daemon-reload failed"
    $SUDO systemctl enable --now "$UNIT"     || err "enable --now failed"
    sleep 3
    $SUDO journalctl -u "$UNIT" --no-pager -n 15
    echo
    echo "state: $($SUDO systemctl is-active "$UNIT") / enabled: $($SUDO systemctl is-enabled "$UNIT")"
    echo "logs : journalctl -u $UNIT -f"
    echo
    echo "Delivery was already confirmed on this box. If a future alert never lands, run:"
    echo "  $0 test-notify"
    echo "and check the topic in $DST/LLM_Status_Watch.conf matches what the app is subscribed to." ;;

  trial)
    # Background trial with the REAL config (10 min threshold) but nothing published.
    # Survives the ssh session; the log is the artifact: which alerts it would have sent.
    need_python
    mkdir -p "$HOME/.local/state/LLM_Status_Watch"
    if pgrep -f "[l]lm-watcher.py --dry-run" >/dev/null; then
      echo "a dry-run trial is already running: $(pgrep -f '[l]lm-watcher.py --dry-run')"
      echo "log: $HOME/.local/state/LLM_Status_Watch/dry-run.log"; exit 0
    fi
    setsid nohup "$PY" "$SRC/LLM_Status_Watch.py" --dry-run \
      >> "$HOME/.local/state/LLM_Status_Watch/dry-run.log" 2>&1 < /dev/null &
    sleep 2
    echo "trial running (pid $(pgrep -f '[l]lm-watcher.py --dry-run' | head -1))"
    echo "  watch : tail -f $HOME/.local/state/LLM_Status_Watch/dry-run.log"
    echo "  stop  : $0 trial-stop" ;;

  trial-stop)
    pkill -f "[l]lm-watcher.py --dry-run" && echo "trial stopped" || echo "no trial was running" ;;

  status)
    systemctl status "$UNIT" --no-pager 2>&1 | head -20 ;;

  logs)
    journalctl -u "$UNIT" --no-pager -n "${2:-50}" --since "${3:-today}" ;;

  stop)    $SUDO systemctl stop "$UNIT" && echo "stopped" ;;
  restart) $SUDO systemctl restart "$UNIT" && echo "restarted" ;;

  uninstall)
    $SUDO systemctl disable --now "$UNIT" 2>/dev/null
    $SUDO rm -f "/etc/systemd/system/$UNIT.service"
    $SUDO systemctl daemon-reload
    echo "service removed. Files left in place: $DST and ~/.local/state/LLM_Status_Watch" ;;

  *)
    sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
esac
