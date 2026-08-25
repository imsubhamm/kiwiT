#!/usr/bin/env bash
set -euo pipefail

base_url=${KIWIT_BASE_URL:-http://127.0.0.1:8000}
curl --fail --silent --show-error --connect-timeout 3 --max-time 20 "$base_url/live" >/dev/null
curl --fail --silent --show-error --connect-timeout 3 --max-time 20 "$base_url/ready" >/dev/null
echo "kiwiT operational checks passed: $base_url"
