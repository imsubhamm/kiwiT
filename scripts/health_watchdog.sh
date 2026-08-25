#!/usr/bin/env bash
set -euo pipefail

base_url=${KIWIT_WATCHDOG_URL:-http://127.0.0.1:8000}
if ! response=$(curl --fail --silent --show-error --connect-timeout 3 --max-time 10 "$base_url/ready" 2>&1); then
  logger -p daemon.crit -t kiwit-watchdog "readiness_alert endpoint=$base_url/ready detail=$response"
  exit 1
fi
logger -p daemon.info -t kiwit-watchdog "readiness_ok endpoint=$base_url/ready"
