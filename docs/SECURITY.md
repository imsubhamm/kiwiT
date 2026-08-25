# Security baseline

kiwiT remains paper-only. Security controls must fail closed and must never bypass risk halts or human approval.

## Implemented controls

- The API binds only to `127.0.0.1`; nginx is the sole network edge.
- API and metrics operations require a constant-time compared key. `KIWIT_PREVIOUS_API_KEY` permits a short, controlled overlap during rotation.
- Host headers are allow-listed, request bodies are limited to 64 KiB, documentation endpoints are disabled, and browser responses prohibit framing and cross-origin asset loading.
- nginx applies per-IP request limits, removes upstream server disclosure, and enforces bounded proxy timeouts.
- systemd runs as the unprivileged `kiwit` user with no capabilities, a read-only filesystem, protected kernel/system resources, and write access only to `/opt/kiwit/shared`.
- CI audits Python dependencies, runs static security analysis, rejects tracked private keys, and Dependabot proposes dependency and Action updates.

## Required infrastructure controls

Allow inbound `443` publicly after TLS is configured. Keep `8000` and PostgreSQL closed. Restrict `22` to known operator addresses or replace SSH deployment with AWS Systems Manager. Port `80` should redirect to HTTPS after a certificate is installed; never transmit an API key over plain HTTP.

Enable IMDSv2-only on EC2, encrypted EBS, automated Neon backups/PITR, AWS account MFA, least-privilege IAM, CloudTrail, GuardDuty, and centralized immutable logs. Put GitHub secrets in the protected `production` environment with required reviewers. Rotate the EC2 deployment key, database credential, and API key after any suspected disclosure.

## API-key rotation

1. Generate a random key of at least 32 bytes and set it as `KIWIT_API_KEY`; temporarily set the old value as `KIWIT_PREVIOUS_API_KEY` in the server environment.
2. Deploy and update authorized clients through a secure channel.
3. Confirm no client uses the old key, remove `KIWIT_PREVIOUS_API_KEY`, and deploy again.

Never log credentials, request authorization headers, database URLs, private keys, broker tokens, or full order payloads. A production broker integration will require distinct read, paper, and execution credentials plus an independent live-execution authorization gate.
