# Monitoring and operations

kiwiT exposes separate operational signals so a running process is not mistaken for a usable trading service.

## Endpoints

- `GET /live`: process liveness; does not touch external dependencies.
- `GET /ready`: checks PostgreSQL connectivity and migration availability. Returns 503 when the control plane must not receive traffic.
- `GET /health`: public service identity and the `paper-only` execution invariant.
- `GET /metrics`: authenticated Prometheus text metrics. Send `X-KIWIT-API-Key`; never place the key in a URL.

Metrics cover uptime, in-flight requests, request totals/status, cumulative request duration, and readiness failures. Request logs are JSON and carry `X-Request-ID` for correlation. A caller-provided request ID is echoed; otherwise kiwiT creates one.

## EC2 operator commands

```bash
sudo systemctl status kiwit-api nginx --no-pager
sudo journalctl -u kiwit-api --since "30 minutes ago" --no-pager
sudo journalctl -u nginx --since "30 minutes ago" --no-pager
curl --fail http://127.0.0.1:8000/live
curl --fail http://127.0.0.1:8000/ready
sudo nginx -t
```

Use `scripts/operations_check.sh` for a single non-secret smoke test. The merge deployment calls `/ready` and fails if PostgreSQL is unavailable.

## Minimum alerts

Alert immediately on readiness failure for 2 consecutive minutes, systemd service failure, or disk usage above 85%. Alert during NSE operating hours when the HTTP 5xx ratio exceeds 2% over five minutes or p95 latency exceeds two seconds. Treat any unexpected live-execution configuration or paper-account risk halt as a safety incident.

Do not automatically resume a halted paper account. Establish the reason, preserve logs and workflow IDs, reconcile positions and ledger entries, then require an identified operator to release the halt.

## Incident sequence

1. Stop new activity with the account halt endpoint when trading state may be unsafe.
2. Record UTC time, request ID, account ID, workflow thread ID, release symlink, and recent service logs.
3. Check `/live`, `/ready`, PostgreSQL health, disk, memory, and nginx status.
4. Roll back by pointing `/opt/kiwit/current` at the preceding release and restarting `kiwit-api`; never roll database migrations backward during an incident.
5. Verify readiness and reconcile paper positions before resuming.

Retain at least five releases (the deploy script does this), ship journald/nginx logs to durable centralized storage before live pilots, and configure EC2/Neon alarms outside the host so an instance failure cannot silence its own alert.
