#!/usr/bin/env bash
set -euo pipefail

base_url=${KIWIT_WATCHDOG_URL:-http://127.0.0.1:8001}
if ! response=$(curl --fail --silent --show-error --connect-timeout 3 --max-time 10 "$base_url/live" 2>&1); then
  logger -p daemon.crit -t kiwit-watchdog "liveness_alert endpoint=$base_url/live detail=$response"
  exit 1
fi
logger -p daemon.info -t kiwit-watchdog "liveness_ok endpoint=$base_url/live"

# This policy reads only the local clock/calendar. /live never queries PostgreSQL.
database_check=$(/opt/kiwit/current/.venv/bin/python -m kiwit.monitoring_schedule)
if [[ $database_check == deferred ]]; then
  logger -p daemon.info -t kiwit-watchdog "database_check_deferred off_hours"
  exit 0
fi
if [[ $database_check != due ]]; then
  logger -p daemon.crit -t kiwit-watchdog "invalid_monitoring_schedule"
  exit 1
fi
if ! response=$(curl --fail --silent --show-error --connect-timeout 3 --max-time 10 "$base_url/ready" 2>&1); then
  logger -p daemon.crit -t kiwit-watchdog "readiness_alert endpoint=$base_url/ready detail=$response"
  exit 1
fi
logger -p daemon.info -t kiwit-watchdog "readiness_ok endpoint=$base_url/ready"

/opt/kiwit/current/.venv/bin/python /opt/kiwit/current/scripts/banknifty_health.py
