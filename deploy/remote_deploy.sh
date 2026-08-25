#!/usr/bin/env bash
set -euo pipefail

archive=${1:?release archive required}
release_root=/opt/kiwit/releases
release_id=$(date -u +%Y%m%d%H%M%S)
release_dir="$release_root/$release_id"

if [[ $EUID -ne 0 ]]; then
  echo "remote_deploy.sh must run through sudo" >&2
  exit 1
fi

install -d -o kiwit -g kiwit -m 0750 "$release_dir"
tar -xzf "$archive" -C "$release_dir"
chown -R kiwit:kiwit "$release_dir"
runuser -u kiwit -- python3 -m venv "$release_dir/.venv"
runuser -u kiwit -- "$release_dir/.venv/bin/python" -m pip install --disable-pip-version-check "$release_dir[api,production,workflow,research]"

set -a
source /etc/kiwit/kiwit.env
set +a
runuser -u kiwit -- env \
  HOME=/opt/kiwit \
  KIWIT_DATABASE_URL="$KIWIT_DATABASE_URL" \
  KIWIT_DB_CONNECT_TIMEOUT="${KIWIT_DB_CONNECT_TIMEOUT:-15}" \
  "$release_dir/.venv/bin/python" "$release_dir/scripts/manage_database.py" migrate --migrations "$release_dir/migrations"

ln -sfn "$release_dir" /opt/kiwit/current
systemctl restart kiwit-api
systemctl reload nginx
sleep 2
curl --fail --silent --connect-timeout 3 --max-time 20 http://127.0.0.1:8000/ready >/dev/null
find "$release_root" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -nr | tail -n +6 | cut -d' ' -f2- | xargs -r rm -rf
echo "deployed $release_id"
