# Gmail webhook

For the selected ngrok HTTPS ingress, start with [NGROK.md](NGROK.md). It keeps the receiver on loopback and avoids the public 8005 forwarding path below.

Endpoint: `POST /webhooks/gmail`, listening directly on **cand5:8080** with `network_mode: host`. Your existing forward is **8005 → 8080**. Compose has no `ports` mappings; containers share cand5's network namespace. The successful host-network hello-world test does not yet establish webhook or image-build readiness.

The public URL must use HTTPS, for example `https://YOUR_HOST:8005/webhooks/gmail`. A plain TCP forward alone does not add TLS. Terminate HTTPS at your existing proxy or tunnel and forward HTTP to 8080. Preserve the Authorization header. The private dashboard remains separate on loopback port 8787.

## What this adds

- Google-signed OIDC token validation: signature, expiration, issuer, exact audience, and verified push service-account email.
- Pub/Sub subscription and Gmail account checks.
- SQLite notification persistence and duplicate handling. HTTP 204 is returned after commit; storage errors return 503 for Pub/Sub retry.
- Notifications wake the existing IMAP collector within approximately one second while it is idle. Email bodies still come from IMAP; a Gmail app password is still required. The ten-second poll remains a fallback.
- Optional Gmail API watch setup and daily renewal, with five-minute retries after renewal errors. This requires OAuth credentials for the same mailbox. It does not replace the IMAP reader with Gmail history synchronization.

## 1. Google Cloud resources

Run these in Google Cloud Shell, not on the small VPS. Replace the project and public URL. The OAuth client used later must belong to this project.

```sh
PROJECT_ID='YOUR_PROJECT_ID'
WEBHOOK_URL='https://YOUR_HOST:8005/webhooks/gmail'
TOPIC='gmail-events'
SUBSCRIPTION='gmail-push'
PUSH_SA="gmail-webhook-push@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"
gcloud services enable gmail.googleapis.com pubsub.googleapis.com iamcredentials.googleapis.com
gcloud pubsub topics create "$TOPIC"
gcloud pubsub topics add-iam-policy-binding "$TOPIC" \
  --member='serviceAccount:gmail-api-push@system.gserviceaccount.com' \
  --role='roles/pubsub.publisher'

gcloud iam service-accounts create gmail-webhook-push
gcloud beta services identity create --service=pubsub.googleapis.com --project="$PROJECT_ID"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud iam service-accounts add-iam-policy-binding "$PUSH_SA" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com" \
  --role='roles/iam.serviceAccountTokenCreator'
```

If resources already exist, reuse them and skip their create commands. The subscription creator also needs `iam.serviceAccounts.actAs` permission on the push service account. Do not create or download a service-account private key.

## 2. Configure and start the receiver

Use the image and deployment bundle installed through [CI-CD.md](CI-CD.md). It already contains the webhook receiver and its dependencies; do not build on cand5. Complete Telegram/Gmail/model setup first. Configure the receiver from the existing project directory:

```sh
cd ~/telegram-mail-test
python3 webhook.py setup
sudo docker compose --profile webhook up -d --no-build webhook
curl -i http://127.0.0.1:8080/healthz
```

Enter the public HTTPS URL, the push service-account email, and the complete subscription name (`projects/PROJECT/subscriptions/gmail-push`). Expected: `WEBHOOK_CONFIG_OK` and HTTP 200 from `/healthz`.

Check `https://YOUR_HOST:8005/healthz` from outside the VPS. HTTP 200 confirms forwarding and TLS only. An unsigned POST to `/webhooks/gmail` must return 401. The webhook container has a 64 MiB RAM limit; all three containers together have configured RAM limits totaling 288 MiB. Actual usage still needs VPS measurement.

For a reverse proxy running inside cand5, optionally set `WEBHOOK_BIND=127.0.0.1` in `.env`. This controls the application listener itself. The default is `0.0.0.0` to support your external port forward. Keep the public HTTPS proxy as the intended ingress. The dashboard explicitly listens on `127.0.0.1:8787` even with host networking.

## 3. Create the push subscription

Back in Cloud Shell:

```sh
gcloud pubsub subscriptions create "$SUBSCRIPTION" \
  --topic="$TOPIC" \
  --push-endpoint="$WEBHOOK_URL" \
  --push-auth-service-account="$PUSH_SA" \
  --push-auth-token-audience="$WEBHOOK_URL" \
  --ack-deadline=30
```

Use the default wrapped Pub/Sub JSON payload. The audience must exactly match the HTTPS URL entered during setup, including the port and path. No API key or secret is placed in the URL.

## 4. Enable Gmail watch

Create an OAuth client and consent configuration in the same Google Cloud project. Authorize the same Gmail account with `https://www.googleapis.com/auth/gmail.readonly` and offline access to obtain a refresh token. Use your own OAuth client if using Google's OAuth Playground. OAuth apps left in external Testing mode can have short-lived refresh tokens; configure the consent app for your intended long-running use.

On the VPS, enter credentials at hidden prompts:

```sh
python3 webhook.py setup-watch
sudo docker compose --profile webhook run --rm --no-deps webhook watch
```

Expected: `GMAIL_OAUTH_CONFIG_OK`, then `GMAIL_WATCH_OK`. Gmail sends an initial notification when watch succeeds. The running receiver checks for renewal every five minutes and renews daily. Credentials are read from the mounted secret directory; they are never logged. A separately managed watch is supported by omitting `setup-watch`. An IMAP app password and a Gemini API key cannot substitute for the Gmail OAuth credentials.

## 5. Verify delivery

Send a new test email, then inspect:

```sh
sudo docker compose exec -T agent python /app/mailagent.py status
sudo docker compose logs --tail=20 webhook
```

Expected: a `gmail_webhook_received` event in the console Logs, `webhook_pending` returning to zero, and the email reaching `sent` with a Telegram message ID. `gmail_watch_expiration` should be in the future. Pending notifications and processing state survive container restarts and SQLite backups.

The receiver writes `gmail_webhook_accepted` (HTTP 204 after commit) or `gmail_webhook_rejected` to Docker logs. Rejections include only the HTTP status, a fixed error code, and an `authenticated` flag. No tokens, headers, notification payloads, or email addresses are logged. For example, `invalid_subscription` or `unexpected_mailbox` identifies a configuration mismatch; `invalid_pubsub_data` or `invalid_history_id` identifies a malformed notification. `unsupported_transfer_encoding` identifies an unsupported request framing. The flag becomes true only after the Google token passes verification; false can also mean a request was rejected before verification.

History IDs supplied as JSON integers or decimal strings are stored exactly as text. Boolean, floating-point, and malformed IDs are rejected. This does not change the IMAP checkpoint or weaken token, subscription, or mailbox validation.

For repeated failures, inspect `pubsub.googleapis.com/subscription/push_request_count` in Cloud Monitoring, grouped by `response_code` and `response_class`, then compare the receiver logs. The default subscription dashboard might not include this chart. A backlog or a Telegram delivery alone does not prove that a push notification was accepted. Pub/Sub retries rejected notifications; do not purge the subscription while diagnosing.

Local tests use generated test keys and mocked Google APIs. They do not establish public reachability, Google authorization, or live end-to-end latency.

Sources: [Gmail push setup and renewal](https://developers.google.com/workspace/gmail/api/guides/push), [Pub/Sub authentication](https://docs.cloud.google.com/pubsub/docs/authenticate-push-subscriptions).
