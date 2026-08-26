#!/usr/bin/env bash
set -euo pipefail

archive=${1:?release archive required}
release_sha=${2:?release SHA required}
release_root=/opt/kiwit/releases
if [[ ! $release_sha =~ ^[0-9a-f]{40}$ ]]; then
  echo "release SHA must be a full Git commit hash" >&2
  exit 1
fi
release_id="$(date -u +%Y%m%d%H%M%S)-${release_sha:0:12}"
release_dir="$release_root/$release_id"
previous_release=$(readlink -f /opt/kiwit/current 2>/dev/null || true)
activated=false

if [[ $EUID -ne 0 ]]; then
  echo "remote_deploy.sh must run through sudo" >&2
  exit 1
fi

exec 9>/run/lock/kiwit-deploy.lock
if ! flock -n 9; then
  echo "another kiwiT deployment is in progress" >&2
  exit 1
fi

rollback() {
  exit_code=$?
  if [[ $activated == true && -n $previous_release && -d $previous_release ]]; then
    echo "readiness failed; rolling back to $previous_release" >&2
    ln -sfn "$previous_release" /opt/kiwit/current
    install -m 0644 "$previous_release/deploy/kiwit-api.service" /etc/systemd/system/kiwit-api.service
    install -m 0644 "$previous_release/deploy/kiwit-intraday.service" /etc/systemd/system/kiwit-intraday.service 2>/dev/null || true
    install -m 0644 "$previous_release/deploy/kiwit-intraday.timer" /etc/systemd/system/kiwit-intraday.timer 2>/dev/null || true
    install -m 0644 "$previous_release/deploy/nginx-kiwit.conf" /etc/nginx/conf.d/kiwit.conf
    systemctl daemon-reload
    systemctl stop kiwit-banknifty.timer kiwit-banknifty.service 2>/dev/null || true
    if [[ -f "$previous_release/deploy/kiwit-banknifty.service" ]]; then
      install -m 0644 "$previous_release/deploy/kiwit-banknifty.service" /etc/systemd/system/kiwit-banknifty.service
      install -m 0644 "$previous_release/deploy/kiwit-banknifty.timer" /etc/systemd/system/kiwit-banknifty.timer
      systemctl daemon-reload
      systemctl start kiwit-banknifty.timer
    fi
    systemctl restart kiwit-api
    systemctl reload nginx
  fi
  exit "$exit_code"
}
trap rollback ERR

install -d -o kiwit -g kiwit -m 0750 "$release_dir"
tar -xzf "$archive" -C "$release_dir"
chown -R kiwit:kiwit "$release_dir"
runuser -u kiwit -- python3 -m venv "$release_dir/.venv"
runuser -u kiwit -- "$release_dir/.venv/bin/python" -m pip install --disable-pip-version-check "$release_dir[api,production,workflow,research]"
printf '%s\n' "$release_sha" > "$release_dir/RELEASE_SHA"
chown kiwit:kiwit "$release_dir/RELEASE_SHA"

set -a
source /etc/kiwit/kiwit.env
set +a
runuser -u kiwit -- env \
  HOME=/opt/kiwit \
  KIWIT_DATABASE_URL="$KIWIT_DATABASE_URL" \
  KIWIT_DB_CONNECT_TIMEOUT="${KIWIT_DB_CONNECT_TIMEOUT:-15}" \
  "$release_dir/.venv/bin/python" "$release_dir/scripts/manage_database.py" migrate --migrations "$release_dir/migrations"

if [[ -n $previous_release && -d $previous_release ]]; then
  activated=true
fi
install -m 0644 "$release_dir/deploy/kiwit-api.service" /etc/systemd/system/kiwit-api.service
install -m 0644 "$release_dir/deploy/nginx-kiwit.conf" /etc/nginx/conf.d/kiwit.conf
install -m 0644 "$release_dir/deploy/kiwit-watchdog.service" /etc/systemd/system/kiwit-watchdog.service
install -m 0644 "$release_dir/deploy/kiwit-watchdog.timer" /etc/systemd/system/kiwit-watchdog.timer
install -m 0644 "$release_dir/deploy/kiwit-intraday.service" /etc/systemd/system/kiwit-intraday.service
install -m 0644 "$release_dir/deploy/kiwit-intraday.timer" /etc/systemd/system/kiwit-intraday.timer
install -m 0644 "$release_dir/deploy/kiwit-banknifty.service" /etc/systemd/system/kiwit-banknifty.service
install -m 0644 "$release_dir/deploy/kiwit-banknifty.timer" /etc/systemd/system/kiwit-banknifty.timer
chmod 0755 "$release_dir/scripts/health_watchdog.sh"
chmod 0755 "$release_dir/scripts/run_intraday_worker.py"
systemctl daemon-reload
systemctl enable --now kiwit-watchdog.timer
systemctl enable --now kiwit-intraday.timer
systemctl daemon-reload
nginx -t
ln -sfn "$release_dir" /opt/kiwit/current
activated=true
systemctl restart kiwit-api
systemctl reload nginx
for attempt in {1..10}; do
  if curl --fail --silent --connect-timeout 3 --max-time 10 http://127.0.0.1:8000/ready >/dev/null; then
    break
  fi
  if [[ $attempt -eq 10 ]]; then
    echo "new release did not become ready" >&2
    false
  fi
  sleep 2
done
systemctl enable --now kiwit-banknifty.timer
trap - ERR
find "$release_root" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -nr | tail -n +6 | cut -d' ' -f2- | xargs -r rm -rf
echo "deployed $release_id ($release_sha)"
