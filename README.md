# Mail Agent

Gmail → English summary → Telegram. A standard-library Python mail worker, SQLite, and a static React console. The optional Pub/Sub receiver uses Google's authentication library. Designed for Ubuntu 24.04 with 512 MiB RAM and 512 MiB swap.

Gmail push notifications on port 8080: see [WEBHOOK.md](WEBHOOK.md) for your 8005 → 8080 forwarding setup.

GitHub builds and publishes images; the server pulls a selected release and updates Docker Compose. See [CI-CD.md](CI-CD.md). No inbound deployment SSH or Actions secrets are required. Live cand5 verification remains pending.

**Target:** cand5, an LXC container nested in KVM. Default Docker networking fails with a sysctl permission error. The user has verified `docker run --rm --network host hello-world` only. Image building, application startup, container access to Gmail/model/Telegram, delivery within 60 seconds, and server reboot recovery remain unverified on cand5.

All Compose services use `network_mode: host` and share cand5's network namespace, not the outer KVM host's network. There are no `ports` mappings. Build steps also request host networking. The dashboard binds directly to `127.0.0.1:8787`; the webhook binds to `${WEBHOOK_BIND:-0.0.0.0}:8080`. SQLite persists in `./data`; credentials remain read-only at `/run/agent-secrets`. See [CAND5.md](CAND5.md) for staged host checks.

## Deployment

Upload from your computer:

```sh
ssh USER@HOST "mkdir -p ~/telegram-mail-test/frontend"
scp -r Dockerfile compose.yaml requirements.txt mailagent.py dashboard.py demo_dashboard.py webhook.py prepare.sh README.md WEBHOOK.md CAND5.md tests USER@HOST:~/telegram-mail-test/
scp -r frontend/dist USER@HOST:~/telegram-mail-test/frontend/
ssh USER@HOST
```

On Ubuntu, skip package installation if Docker is already installed:

```sh
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 python3
sudo systemctl enable --now docker
sudo timedatectl set-ntp true
cd ~/telegram-mail-test
sh prepare.sh
```

Expected: `READY`.

Build the shared image once before verifying service access from containers:

```sh
sudo docker compose --profile webhook config --quiet
sudo docker compose build agent
```

Proceed only if both commands exit successfully. Host networking for service startup does not by itself validate the image build or its builder permissions.

### 1. Telegram

```sh
python3 mailagent.py setup-telegram
sudo docker compose run --rm --no-deps agent verify-telegram
```

Enter the token at the hidden prompt. Expected: `TELEGRAM_SEND_OK` and a delivery test in Telegram. The bot must be `@Tommy_test_mail_bot`; the private chat is `7558581320`. Start the private conversation first. HTTP 401 generally indicates an invalid token; 403 may indicate a blocked bot or missing conversation.

### 2. Gmail

```sh
python3 mailagent.py setup-gmail
sudo docker compose run --rm --no-deps agent verify-gmail
```

Use a Google app password with two-step verification, not your account password. Accounts that prohibit app passwords require a separate OAuth integration. Expected: `GMAIL_OK`.

The first verification establishes a checkpoint. Only subsequent INBOX arrivals are processed, including read messages. Historical messages, spam, and messages moved out of INBOX before polling are excluded. Reverification and restore preserve the checkpoint. IMAP access is read-only.

### 3. Model

```sh
python3 mailagent.py setup-model
sudo docker compose run --rm --no-deps agent verify-model
```

Enter the full HTTPS Chat Completions endpoint, model ID, and API key. The endpoint must support `tools`, `tool_choice=required`, and `max_tokens`.

Expected: `MODEL_TOOL_LOOP_OK: read_mail -> submit_summary` and an English summary of synthetic test mail. Real operation sends bounded mail content to the configured model provider.

### 4. Services

```sh
python3 dashboard.py setup
sudo docker compose up -d --no-build
sudo docker compose ps
sudo docker compose exec -T agent python /app/mailagent.py health
```

Expected: `healthy` and `HEALTH_OK`. The services are configured to continue after SSH disconnect and restart after a VPS reboot; verify both on cand5 using the tests below. A manually stopped service needs `compose up -d` to enable it again.

Memory limits: agent 160 MiB RAM / 224 MiB RAM plus swap; console 64 MiB RAM / 80 MiB RAM plus swap. Measure actual usage with `sudo docker stats --no-stream`. Node is not required on the VPS.

## Console

From your computer:

```sh
ssh -N -L 8787:127.0.0.1:8787 USER@HOST
```

Open [Mail Agent](http://127.0.0.1:8787) and enter the dashboard password. The HTTP service is for local or SSH-tunneled access; do not expose it directly to the internet. Sessions expire after 12 hours or service restart.

Pages: **Overview**, **Emails**, **Prompt**, **Model / API**, **Logs**.

`Save draft` preserves the active configuration. `Test & apply` tests the tool loop with a synthetic email, using the configured API key, before activation. It incurs a small API call cost. New settings apply to the next summary; retries reuse existing summaries.

Keys are entered only through `python3 mailagent.py setup-model`. The console never returns keys or raw email bodies. Update the key before switching providers. Reverify and reapply settings after changing credentials or restoring a backup.

To start the console before configuring the agent:

```sh
python3 dashboard.py setup
sudo docker compose up -d --build dashboard
```

## Live acceptance tests

Check clock synchronization with `timedatectl status`. Send three uniquely numbered emails, about 30 seconds apart. Run:

```sh
sudo docker compose exec -T agent python /app/mailagent.py status
sudo docker stats --no-stream
```

Expected for each: `state=sent`, a `telegram_id`, and `gmail_to_telegram_seconds <= 60`. Match the email ID with the Telegram message. Timing starts at Gmail INTERNALDATE and ends at Telegram API acknowledgement, not phone notification display.

The agent polls every 10 seconds and uses one worker. Bursts, slow APIs, throttling, outages, and retries can exceed the 60-second target. The tool loop allows four turns with a 32-second scheduling budget and per-request socket timeouts up to 12 seconds; this is not a strict wall-clock SLA.

Disconnect SSH, send another email, and verify delivery.

Test container interruption:

```sh
sudo docker compose kill -s SIGKILL agent
sudo docker compose up -d
sudo docker compose exec -T agent python /app/mailagent.py status
```

Completed records should keep their Telegram IDs. Interrupted tasks should recover and retry. A crash between Telegram acceptance and SQLite commit can duplicate a message with the same email ID.

Test server recovery: save status output, run `sudo reboot`, and send an email during the outage. Reconnect without manually starting containers:

```sh
cd ~/telegram-mail-test
sudo docker compose ps
sudo docker compose exec -T agent python /app/mailagent.py status
```

Expected: services restart, become healthy, and process queued mail. Send another email to measure normal post-reboot latency. Outage time is excluded from the normal-operation target.

## Reliability and storage

- SQLite uses WAL and FULL synchronization. Jobs commit before the IMAP cursor advances. Gmail X-GM-MSGID provides deduplication; UIDVALIDITY changes trigger a rescan.
- States: `pending → processing → sending → sent`; errors enter `retry`. Attempts, error codes, events, and retry times persist.
- Retry backoff rises to about 302 seconds, or longer when required by Retry-After. Failed jobs remain queued.
- Delivery is **at least once**, not exactly once. Telegram and SQLite cannot share a transaction.
- The model must call `read_mail`, receive the tool result, then call `submit_summary`. Tools cannot execute commands, browse links, access credentials, or change chat destinations.
- Fetches are limited to 256 KiB and parsed text to 20,000 characters. Attachments are not processed. Truncation can hide content.
- Successful delivery clears stored bodies. Summaries, subjects, senders, and events remain for 30 days. Compact deduplication records remain indefinitely. Failed jobs retain their body until success. Database deletion is not secure erasure.
- Credentials live in `secrets/*.json`, with directory mode 700 and file mode 600 on Linux. They are excluded from Git and images and mounted read-only. Bind mounts do not encrypt secrets at rest.
- Health checks report stale heartbeats. Docker does not restart a container merely because it is unhealthy. Application logic reconnects and retries; Docker restarts exited processes.

## Backup and restore

Create a consistent online snapshot:

```sh
python3 mailagent.py backup "backups/agent-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
```

Expected: `BACKUP_OK`. Copy it to another trusted machine with `scp`. It contains private mail data, checkpoints, settings, and retries, but no credentials. Use this command instead of copying a live database without its WAL.

For migration, stop the old agent before the final snapshot and keep it stopped. Older snapshots may replay messages sent after the snapshot.

On the new host, before other setup or status commands:

```sh
sh prepare.sh
python3 mailagent.py restore /path/to/snapshot.sqlite3
python3 mailagent.py setup-telegram
python3 mailagent.py verify-telegram
python3 mailagent.py setup-gmail
python3 mailagent.py verify-gmail
python3 mailagent.py setup-model
python3 mailagent.py verify-model
python3 dashboard.py setup
sudo docker compose up -d --build
```

Expected: `RESTORE_OK` and preserved progress. Restore requires an empty data directory and the same Gmail account. A kernel lock prevents duplicate workers on one host; active multi-host operation is unsupported.

## Development

```sh
cd frontend
npm ci
npm run build
cd ..
python dashboard.py serve --demo
```

The read-only localhost demo uses sample data and makes no external API calls. Rebuild after frontend changes. Prebuilt files in `frontend/dist` let the VPS run without Node.

```sh
python -m unittest discover -s tests -v
sudo docker compose config --quiet
```

Tests cover crash recovery, deduplication, snapshots, retries, tool feedback, authentication, origin checks, and atomic settings activation. Mock API responses are not evidence of live delivery latency.

References: [Telegram](https://core.telegram.org/bots/api#sendmessage), [Gmail IMAP](https://developers.google.com/workspace/gmail/imap/imap-extensions), [App passwords](https://support.google.com/mail/answer/185833), [Compose](https://docs.docker.com/reference/compose-file/services/).
