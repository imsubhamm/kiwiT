#!/usr/bin/env bash
set -euo pipefail
# Certbot deploy hook: only reload after validating the active configuration.
/usr/sbin/nginx -t
/usr/bin/systemctl reload nginx
