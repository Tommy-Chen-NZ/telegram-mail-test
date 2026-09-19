# ngrok HTTPS ingress

This publishes only the Gmail webhook on cand5 port 8080. The tunnel uses a separate Compose project, host networking, restart unless-stopped, a digest-pinned official ngrok image, and a read-only mount containing only its own credential. No Google or application credentials are mounted into ngrok. The configured RAM limit is 64 MiB (96 MiB including swap); real cand5 memory use must be measured.

Deployment account: use the same Linux account and project directory as the existing agent. Current Google Cloud project: `storied-precept-509003-e1`. Assigned public domain: `gully-gallantly-luckless.ngrok-free.dev`.

## 1. Update and configure

Wait for the new Actions checks and publication to succeed. Run `scripts/pull-release.sh IMAGE deploy` with the full published digest or commit tag, as described in [CI-CD.md](CI-CD.md). The update supplies `ngrok_setup.py` and `compose.ngrok.yaml` without starting a tunnel or changing stored credentials.

```sh
cd ~/telegram-mail-test
python3 ngrok_setup.py setup
```

Enter the assigned domain, Google project ID, and ngrok **Authtoken** at its hidden prompt. Get the token from the ngrok dashboard; do not paste it into chat, shell arguments, or Compose. Expected: `NGROK_CONFIG_OK`.

The helper writes private `secrets/ngrok/config.yml` and `secrets/webhook.json`, and sets `WEBHOOK_BIND=127.0.0.1` while preserving other .env settings. It expects the Pub/Sub service account `gmail-webhook-push` and subscription `gmail-push` in the selected project. An existing conflicting webhook configuration is rejected for review. Runtime config contains no additional ngrok authentication or traffic policies. Existing Google-signed OIDC verification stays in the webhook receiver.

Local ngrok traffic inspection and the local web UI are disabled. Docker logging is disabled for the tunnel because vendor authentication errors can echo rejected tokens. This does not control ngrok's cloud-side observability settings. Keep request/header/body capture disabled in your ngrok account; use the verification helper and container status for diagnostics.

## 2. Start the upstream before the tunnel

```sh
sudo docker compose --profile webhook up -d --no-build webhook
curl --fail --max-time 10 -i http://127.0.0.1:8080/healthz
```

Expected: HTTP 200 with an empty body and `Content-Type: application/json`. Stop here if the local check fails. No Gmail OAuth authorization or Pub/Sub cloud resources are required for this transport health check.

```sh
sudo docker compose -p telegram-mail-tunnel -f compose.ngrok.yaml pull
sudo docker compose -p telegram-mail-tunnel -f compose.ngrok.yaml up -d --no-build
python3 ngrok_setup.py verify
sudo docker compose -p telegram-mail-tunnel -f compose.ngrok.yaml ps
sudo docker stats --no-stream
```

Always include `-p telegram-mail-tunnel`: the main project's COMPOSE_PROJECT_NAME in .env otherwise overrides the tunnel file's project name. Leave the container running after verification. Use `sudo docker compose -p telegram-mail-tunnel -f compose.ngrok.yaml stop` to stop it; a manually stopped container will not restart at boot.

Expected verification markers: `LOCAL_WEBHOOK_OK`, `PUBLIC_WEBHOOK_OK`, `UNSIGNED_WEBHOOK_REJECTED`, and `TUNNEL_VERIFIED`. The helper compares the public /healthz response to the exact local result and confirms an unsigned POST returns HTTP 401 with the application's unauthorized response, rather than an ngrok error/interstitial page. These checks prove transport and rejection only, not a signed Google delivery.

## 3. Configure Google delivery

Follow [WEBHOOK.md](WEBHOOK.md) using:

```sh
PROJECT_ID='storied-precept-509003-e1'
WEBHOOK_URL='https://gully-gallantly-luckless.ngrok-free.dev/webhooks/gmail'
```

Create the topic, publisher binding, push service account and authenticated subscription. Authorize Gmail OAuth for the same mailbox, then configure and verify watch renewal. You can skip `webhook.py setup` because the ngrok helper has written that configuration. The public endpoint and OIDC audience must use the identical HTTPS URL. Do not add browser-login middleware to this machine-to-machine endpoint.

The external 8005 forwarding is not used by this route. After a signed notification succeeds, test a real email, persistent deduplication, SSH disconnect and server reboot with the same public URL. Do not report these passed until actual results exist. The receiver still uses IMAP to fetch mail and polling remains a fallback.

The tunnel runs independently of application rollouts. To apply a future tunnel image/configuration change, rerun its `pull` and `up -d` commands, then `ngrok_setup.py verify`. Keep its directory and credential on the server; never commit them. Free ngrok plans have account traffic/request limits; monitor the dashboard and retain the mailbox polling fallback.

References: [Agent configuration](https://ngrok.com/docs/gateway/agent/config/v3), [Free plan limits](https://ngrok.com/docs/pricing-limits/free-plan-limits).
