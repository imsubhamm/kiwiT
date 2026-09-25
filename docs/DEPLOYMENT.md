# EC2 and GitHub Deployment

The API runs as an unprivileged `kiwit` systemd service on `127.0.0.1:8001`; Nginx is the public listener. GitHub Actions runs tests on pull requests and deploys every commit merged or pushed to `main`.

## One-time EC2 bootstrap

The EC2 key must authenticate first:

```bash
chmod 600 ~/.ssh/tathyasm.pem
ssh -i ~/.ssh/tathyasm.pem ubuntu@16.16.97.239
```

Then clone the repository and run:

```bash
git clone https://github.com/imsubhamm/kiwiT.git
cd kiwiT
sudo bash deploy/bootstrap_ec2.sh
```

The security group should allow SSH only from the operator/GitHub runner strategy selected, HTTP temporarily, and HTTPS publicly after a domain and certificate are configured. Port 8001 must not be public.

## GitHub production secrets

- `EC2_HOST`: public IP or stable Elastic IP.
- `EC2_USER`: AMI SSH username.
- `EC2_SSH_KEY`: full private deploy key.
- `EC2_KNOWN_HOSTS`: pinned `ssh-keyscan` output captured through a trusted first connection; deployment never trusts a freshly scanned key.
- `KIWIT_DATABASE_URL`: Neon TLS connection string.
- `KIWIT_API_KEY`: random value of at least 24 characters.

Protect the `production` environment with required reviewers. Prefer a dedicated restricted deployment key or AWS Systems Manager over a general instance key. Add HTTPS before using the dashboard beyond a private test environment; API keys must not travel over plain HTTP.

The deploy serializes releases with a host lock, uses the full Git SHA as release identity, migrates before switching the `current` symlink, validates nginx, and automatically restores the previous application/configuration when readiness fails. It keeps five releases and verifies both public readiness and the deployed SHA.

The public `/health` response reports the active commit in `release`. PostgreSQL migrations are forward-only and are not reverted during application rollback.

## Manual rollback

Automatic rollback covers failed startup/readiness. For an operator-directed rollback:

```bash
ls -1dt /opt/kiwit/releases/*
sudo ln -sfn /opt/kiwit/releases/<previous-release> /opt/kiwit/current
sudo install -m 0644 /opt/kiwit/current/deploy/kiwit-api.service /etc/systemd/system/kiwit-api.service
sudo install -m 0644 /opt/kiwit/current/deploy/nginx-kiwit.conf /etc/nginx/conf.d/kiwit.conf
sudo systemctl daemon-reload
sudo nginx -t
sudo systemctl restart kiwit-api
sudo systemctl reload nginx
curl --fail http://127.0.0.1:8001/ready
```

The shared EC2 host reserves port 8001 for KiwiT; port 8000 belongs to another application. Releases preserve `/etc/kiwit/kiwit.env`; provision or rotate application secrets on the host separately. GitHub needs only the SSH deployment secrets. The deploy script updates the release identity and backs up host configuration for rollback.

## Production options event calendar

Options workers fail closed without a verified event calendar. Keep the mutable calendar outside
release directories and make it readable by the unprivileged service account:

```bash
sudo install -d -o root -g kiwit -m 0750 /etc/kiwit/calendar
sudo install -o root -g kiwit -m 0640 options-event-calendar.json \
  /etc/kiwit/calendar/options-event-calendar.json
sudo sed -i '/^KIWIT_OPTIONS_EVENT_CALENDAR=/d' /etc/kiwit/kiwit.env
echo 'KIWIT_OPTIONS_EVENT_CALENDAR=/etc/kiwit/calendar/options-event-calendar.json' | \
  sudo tee -a /etc/kiwit/kiwit.env >/dev/null
```

Use `config/options-event-calendar.example.json` as the schema. Every update needs an owner,
an official HTTPS source, explicit coverage dates and event-specific sources. An empty event list
is valid only when the cited source verifies the whole coverage period has no relevant events.
Install updates atomically by writing a separate file, validating it, then moving it into place:

```bash
sudo -u kiwit /opt/kiwit/current/.venv/bin/python \
  /opt/kiwit/current/scripts/validate_options_event_calendar.py \
  --path /etc/kiwit/calendar/options-event-calendar.next.json
sudo mv /etc/kiwit/calendar/options-event-calendar.next.json \
  /etc/kiwit/calendar/options-event-calendar.json
```

Deployment validates the configured file as `kiwit` before migrations or activation. Missing,
unreadable, stale, uncovered or malformed calendars abort deployment and restore the previous release.
