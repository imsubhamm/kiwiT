# kiwit.tathyaforge.in HTTPS setup

DNS stays with Hostinger. Do not migrate nameservers or edit apex, www, MX, SPF, DKIM or DMARC records.

## DNS prerequisite

The owner added A record `kiwit` → `13.201.76.17`, TTL 14400. The record was verified from EC2 and a public resolver. Hostinger browser automation was blocked by its browser policy check; no other DNS records were edited by this task.

EC2's security group must allow incoming TCP 80 (ACME renewal) and 443 (HTTPS). Port 80 was reachable; port 443 must be checked once TLS is listening. Do not broaden SSH access.

## Server stages

1. Install Certbot from Ubuntu's packages.
2. Install `nginx-kiwit-domain-http.conf` as `/etc/nginx/conf.d/kiwit-domain.conf`, retaining `/etc/nginx/conf.d/kiwit.conf` for the working temporary tunnel. The bootstrap vhost serves only ACME challenges; other requests return 503, not a plaintext login.
3. Create `/var/www/kiwit-acme/.well-known/acme-challenge`. Run `nginx -t` before reload.
4. Once DNS resolves correctly, issue a certificate:

```sh
sudo certbot certonly --webroot -w /var/www/kiwit-acme \
  --cert-name kiwit.tathyaforge.in -d kiwit.tathyaforge.in \
  --agree-tos --non-interactive --email hellow@tathyaforge.in
```

5. Add `kiwit.tathyaforge.in` to `KIWIT_ALLOWED_HOSTS` in the deployment workflow and server environment. Preserve existing localhost/tunnel hosts. Restart the API after updating the environment.
6. Back up the bootstrap config; replace it with `nginx-kiwit-domain-tls.conf`, validate, and reload. This separate domain config is not overwritten by `remote_deploy.sh`.
7. Install `renew-kiwit-certificate.sh` under `/etc/letsencrypt/renewal-hooks/deploy/` and enable `certbot.timer`. Run `certbot renew --dry-run` and verify the hook independently.
8. Verify public HTTPS certificate, health/release, login redirect, secure cookies, HTTP→HTTPS redirect and 443 reachability. Then update `KIWIT_DASHBOARD_URL` and the local-preview link to the fixed URL in Git and on EC2.
9. Keep the Quick Tunnel running during cutover. Do not claim the fixed domain is active until public checks pass.

The hostname will be stable, but the current EC2 public IP can change after a stop/start. Update DNS if that happens; an Elastic IP is a separate infrastructure/cost decision.

## Cutover verification (26 August 2026)

- Let's Encrypt certificate issued for `kiwit.tathyaforge.in`, initial expiry 24 November 2026.
- TLS vhost installed and nginx configuration validated; localhost TLS verification succeeded (no certificate bypass).
- Automatic renewal timer is enabled and nginx deploy hook installed.
- Domain added to deployment host allowlist; dashboard/email links now target `https://kiwit.tathyaforge.in/dashboard`.
- Owner enabled HTTPS/TCP 443 in the EC2 security group. Public HTTPS health returned 200, `/dashboard` redirected to `/login` (303), login returned 200, and HTTP redirected to HTTPS (308). Certificate verification succeeded without disabling TLS checks.
- Renewal dry run succeeded; nginx renewal hook independently validated and reloaded the configuration successfully.
- Local resolver initially retained a negative DNS response; subsequent unforced HTTPS requests resolved and succeeded. Some clients may need their negative DNS cache to expire.
- Existing Quick Tunnel retained during cutover. The old bootstrap config is backed up at `/etc/nginx/kiwit-domain.bootstrap.conf`.
