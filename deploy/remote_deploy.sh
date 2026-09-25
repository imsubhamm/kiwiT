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
drained=false
units=(kiwit-watchdog kiwit-intraday kiwit-banknifty kiwit-banknifty-supervisor kiwit-banknifty-observer kiwit-banknifty-reports)

if [[ $EUID -ne 0 ]]; then
  echo "remote_deploy.sh must run through sudo" >&2
  exit 1
fi

exec 9>/run/lock/kiwit-deploy.lock
if ! flock -n 9; then
  echo "another kiwiT deployment is in progress" >&2
  exit 1
fi

backup_dir=$(mktemp -d /opt/kiwit/deploy-backup.XXXXXX)
cp /etc/kiwit/kiwit.env "$backup_dir/kiwit.env"
cp /etc/systemd/system/kiwit-api.service "$backup_dir/kiwit-api.service"
cp /etc/systemd/system/kiwit-watchdog.service "$backup_dir/kiwit-watchdog.service"
cp /etc/systemd/system/kiwit-watchdog.timer "$backup_dir/kiwit-watchdog.timer"
cp /etc/nginx/conf.d/kiwit.conf "$backup_dir/kiwit.conf"
for unit in "${units[@]}"; do
  for suffix in timer service; do
    [[ ! -f /etc/systemd/system/$unit.$suffix ]] || cp "/etc/systemd/system/$unit.$suffix" "$backup_dir/"
  done
  systemctl is-enabled "$unit.timer" > "$backup_dir/$unit.enabled" 2>/dev/null || true
  systemctl is-active "$unit.timer" > "$backup_dir/$unit.active" 2>/dev/null || true
done

rollback() {
  exit_code=$?
  if [[ $activated == true && $drained == false ]]; then
    # Drain timeout: the original workers are still running; do not terminate them.
    for unit in "${units[@]}"; do
      if [[ $(cat "$backup_dir/$unit.active") == active ]]; then systemctl start "$unit.timer"; fi
    done
    exit "$exit_code"
  fi
  if [[ $activated == true && -n $previous_release && -d $previous_release ]]; then
    echo "readiness failed; rolling back to $previous_release" >&2
    # Stop every new-generation worker before restoring the old code or environment.
    for unit in "${units[@]}"; do
      systemctl disable --now "$unit.timer" 2>/dev/null || true
      systemctl stop "$unit.service" 2>/dev/null || true
    done
    systemctl stop kiwit-api
    ln -sfn "$previous_release" /opt/kiwit/current
    install -m 0644 "$backup_dir/kiwit-api.service" /etc/systemd/system/kiwit-api.service
    install -m 0644 "$backup_dir/kiwit-watchdog.service" /etc/systemd/system/kiwit-watchdog.service
    install -m 0644 "$backup_dir/kiwit-watchdog.timer" /etc/systemd/system/kiwit-watchdog.timer
    install -o root -g kiwit -m 0640 "$backup_dir/kiwit.env" /etc/kiwit/kiwit.env
    install -m 0644 "$backup_dir/kiwit.conf" /etc/nginx/conf.d/kiwit.conf
    for unit in "${units[@]}"; do
      for suffix in timer service; do
        if [[ -f $backup_dir/$unit.$suffix ]]; then
          install -m 0644 "$backup_dir/$unit.$suffix" "/etc/systemd/system/$unit.$suffix"
        else
          rm -f "/etc/systemd/system/$unit.$suffix"
        fi
      done
    done
    systemctl daemon-reload
    systemctl restart kiwit-api
    systemctl reload nginx
    for unit in "${units[@]}"; do
      if [[ $(cat "$backup_dir/$unit.enabled") == enabled ]]; then systemctl enable "$unit.timer"; fi
      if [[ $(cat "$backup_dir/$unit.active") == active ]]; then systemctl start "$unit.timer"; fi
    done
  fi
  exit "$exit_code"
}
trap rollback ERR

install -d -o kiwit -g kiwit -m 0750 "$release_dir"
tar -xzf "$archive" -C "$release_dir"
chown -R kiwit:kiwit "$release_dir"
runuser -u kiwit -- python3 -m venv "$release_dir/.venv"
runuser -u kiwit -- "$release_dir/.venv/bin/python" -m pip install --disable-pip-version-check -c "$release_dir/requirements.lock" "$release_dir[api,production,workflow,research,ml,llm]"
printf '%s\n' "$release_sha" > "$release_dir/RELEASE_SHA"
chown kiwit:kiwit "$release_dir/RELEASE_SHA"

# Pause scheduling, then let in-flight one-shot workers finish before schema/code changes.
activated=true
for unit in "${units[@]}"; do systemctl stop "$unit.timer" 2>/dev/null || true; done
for attempt in {1..120}; do
  busy=false
  for unit in "${units[@]}"; do
    state=$(systemctl show "$unit.service" -p ActiveState --value 2>/dev/null || true)
    if [[ $state == active || $state == activating || $state == deactivating ]]; then busy=true; fi
  done
  if [[ $busy == false ]]; then break; fi
  if [[ $attempt -eq 120 ]]; then echo "workers failed to drain; deployment aborted" >&2; false; fi
  sleep 1
done
drained=true
systemctl stop kiwit-api

set -a
source /etc/kiwit/kiwit.env
# This root-only file is never loaded by application services.
if [[ -f /etc/kiwit/migration.env ]]; then
  source /etc/kiwit/migration.env
fi
set +a
calendar_path=${KIWIT_OPTIONS_EVENT_CALENDAR:-}
if [[ -z $calendar_path ]]; then
  echo "KIWIT_OPTIONS_EVENT_CALENDAR is required; refusing to start options workers" >&2
  false
fi
runuser -u kiwit -- env \
  KIWIT_OPTIONS_EVENT_CALENDAR="$calendar_path" \
  "$release_dir/.venv/bin/python" "$release_dir/scripts/validate_options_event_calendar.py" \
  --path "$calendar_path"
runuser -u kiwit -- env \
  HOME=/opt/kiwit \
  KIWIT_DATABASE_URL="${KIWIT_MIGRATION_DATABASE_URL:-$KIWIT_DATABASE_URL}" \
  KIWIT_DB_CONNECT_TIMEOUT="${KIWIT_DB_CONNECT_TIMEOUT:-15}" \
  "$release_dir/.venv/bin/python" "$release_dir/scripts/manage_database.py" migrate --migrations "$release_dir/migrations"

if [[ -n ${KIWIT_MIGRATION_DATABASE_URL:-} ]]; then
  "$release_dir/.venv/bin/python" "$release_dir/scripts/provision_database_roles.py" --refresh-grants
fi
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
for worker in supervisor observer reports; do
  install -m 0644 "$release_dir/deploy/kiwit-banknifty-$worker.service" "/etc/systemd/system/kiwit-banknifty-$worker.service"
  install -m 0644 "$release_dir/deploy/kiwit-banknifty-$worker.timer" "/etc/systemd/system/kiwit-banknifty-$worker.timer"
done
systemctl daemon-reload
nginx -t
ln -sfn "$release_dir" /opt/kiwit/current
activated=true
# The server environment may carry a previous release SHA; use the archived identity.
sed -i "/^KIWIT_RELEASE_SHA=/d" /etc/kiwit/kiwit.env
printf 'KIWIT_RELEASE_SHA=%s\n' "$release_sha" >> /etc/kiwit/kiwit.env
systemctl restart kiwit-api
systemctl reload nginx
for attempt in {1..10}; do
  if curl --fail --silent --connect-timeout 3 --max-time 10 http://127.0.0.1:8001/ready >/dev/null; then
    break
  fi
  if [[ $attempt -eq 10 ]]; then
    echo "new release did not become ready" >&2
    false
  fi
  sleep 2
done
systemctl enable --now kiwit-banknifty.timer
systemctl enable --now kiwit-watchdog.timer kiwit-intraday.timer
for worker in supervisor observer reports; do
  systemctl enable --now "kiwit-banknifty-$worker.timer"
done
trap - ERR
find "$release_root" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -nr | tail -n +6 | cut -d' ' -f2- | xargs -r rm -rf
echo "deployed $release_id ($release_sha)"
