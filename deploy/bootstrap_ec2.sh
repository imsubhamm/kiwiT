#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "run as root: sudo bash deploy/bootstrap_ec2.sh" >&2
  exit 1
fi

if command -v dnf >/dev/null 2>&1; then
  dnf install -y python3.12 python3.12-pip nginx git tar
elif command -v apt-get >/dev/null 2>&1; then
  apt-get update
  apt-get install -y python3 python3-venv python3-pip nginx git tar
else
  echo "unsupported distribution" >&2
  exit 1
fi

id -u kiwit >/dev/null 2>&1 || useradd --system --home /opt/kiwit --shell /sbin/nologin kiwit
install -d -o kiwit -g kiwit -m 0750 /opt/kiwit/shared /opt/kiwit/releases
install -d -o root -g kiwit -m 0750 /etc/kiwit
install -m 0644 deploy/kiwit-api.service /etc/systemd/system/kiwit-api.service
install -m 0644 deploy/nginx-kiwit.conf /etc/nginx/conf.d/kiwit.conf
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
systemctl daemon-reload
systemctl enable kiwit-api nginx
nginx -t

echo "Bootstrap complete. Create /etc/kiwit/kiwit.env with mode 0640, then deploy."
