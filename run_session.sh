#!/usr/bin/env bash
# Run the order monitor and the opportunity scanner in one session.
# The monitor polls every 60s. The scanner polls every 5 min, or every 60s
# during the Europe/Ljubljana session-open windows, until the deadline.
# Both are then asked to finish the current cycle and exit.
set -u
cd "$(dirname "$0")"

MINUTES="${1:-28}"
if [ -z "${MINUTES}" ]; then
  MINUTES=28
fi
if ! [[ "$MINUTES" =~ ^[0-9]+$ ]] || [ "$MINUTES" -lt 1 ] || [ "$MINUTES" -gt 30 ]; then
  echo "Trajanje mora biti med 1 in 30 minutami." >&2
  exit 2
fi

export PYTHONUNBUFFERED=1

python3 monitor.py &
MPID=$!
python3 scanner.py &
SPID=$!

cleanup() {
  kill "$MPID" "$SPID" 2>/dev/null || true
  wait "$MPID" 2>/dev/null || true
  wait "$SPID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

deadline=$((SECONDS + MINUTES * 60))
status=0
while [ "$SECONDS" -lt "$deadline" ]; do
  if ! kill -0 "$MPID" 2>/dev/null; then
    wait "$MPID" || status=$?
    echo "Monitor se je ustavil (koda ${status})." >&2
    break
  fi
  if ! kill -0 "$SPID" 2>/dev/null; then
    wait "$SPID" || status=$?
    echo "Skener se je ustavil (koda ${status})." >&2
    break
  fi
  sleep 5
done

exit "$status"
