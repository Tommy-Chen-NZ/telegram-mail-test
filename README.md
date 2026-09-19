# Mail Agent

Gmail → English summary → Telegram. A standard-library Python mail worker with SQLite and terminal-based management. The optional Pub/Sub receiver uses Google's authentication library. Designed for Ubuntu 24.04 with 512 MiB RAM and 512 MiB swap.

Gmail push notifications on port 8080: see [WEBHOOK.md](WEBHOOK.md) for your 8005 → 8080 forwarding setup.

GitHub builds and publishes images; the server pulls a selected release and updates Docker Compose. See [CI-CD.md](CI-CD.md). No inbound deployment SSH or Actions secrets are required. Live cand5 verification remains pending.

For a fixed HTTPS Pub/Sub endpoint through ngrok, see [NGROK.md](NGROK.md).

**Target:** cand5, an LXC container nested in KVM. Default Docker networking fails with a sysctl permission error, so all services use host networking. The existing IMAP/model/Telegram pipeline has delivered test messages in 9.41 and 7.8 seconds, and authenticated Pub/Sub requests have returned HTTP 204 through ngrok. The Gmail API reader recorded a 24.14-second test delivery, and the user confirmed receipt of a separate Telegram delivery check. Sustained latency, resource usage, off-host restore, and cand5 reboot recovery still require live validation. Application images are built in GitHub Actions and pulled by cand5.

All Compose services use `network_mode: host` and share cand5's network namespace, not the outer KVM host's network. There are no `ports` mappings. Build steps also request host networking. The webhook binds to `${WEBHOOK_BIND:-0.0.0.0}:8080`. SQLite persists in `./data`; credentials remain read-only at `/run/agent-secrets`. See [CAND5.md](CAND5.md) for staged host checks.

## Deployment

Upload from your computer:

```sh
ssh USER@HOST "mkdir -p ~/telegram-mail-test"
scp -r Dockerfile compose.yaml requirements.txt mailagent.py gmail_api.py webhook.py prepare.sh README.md WEBHOOK.md CAND5.md tests USER@HOST:~/telegram-mail-test/
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
sudo docker compose up -d --no-build
sudo docker compose ps
sudo docker compose exec -T agent python /app/mailagent.py health
```

Expected: `healthy` and `HEALTH_OK`. The services are configured to continue after SSH disconnect and restart after a VPS reboot; verify both on cand5 using the tests below. A manually stopped service needs `compose up -d` to enable it again.

Memory limits: agent 160 MiB RAM / 224 MiB RAM plus swap; webhook 64 MiB RAM / 80 MiB RAM plus swap; ngrok 64 MiB RAM / 96 MiB RAM plus swap. Measure actual usage with `sudo docker stats --no-stream`. There is no frontend or web management service.

## Terminal management

```sh
sudo docker compose exec -T agent python /app/mailagent.py status
sudo docker compose logs --tail=30 agent webhook
```

Model keys and settings are entered through `python3 mailagent.py setup-model`, followed by `verify-model`. Existing active model and prompt settings in SQLite remain usable after removing the console. No application listener runs on port 8787.

## Live acceptance tests

Check clock synchronization with `timedatectl status`. Send three uniquely numbered emails, about 30 seconds apart. Run:

```sh
sudo docker compose exec -T agent python /app/mailagent.py status
sudo docker stats --no-stream
```

Expected for each: `state=sent`, a `telegram_id`, and `gmail_to_telegram_seconds <= 60`. Match the email ID with the Telegram message. Timing starts at Gmail INTERNALDATE and ends at Telegram API acknowledgement, not phone notification display.

Existing installations use a ten-second IMAP poll and one worker. After configuring Gmail OAuth and verifying Pub/Sub, [switch to the Gmail API reader](GMAIL-API.md) to remove the app-password dependency. API mode uses push wakeups and a sixty-second history reconciliation fallback. Bursts, slow APIs, throttling, outages, and retries can exceed the 60-second target. The tool loop allows four turns with a 32-second scheduling budget and per-request socket timeouts up to 12 seconds; this is not a strict wall-clock SLA.

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

For an IMAP installation, on the new host before other setup or status commands (API-mode backups instead use the OAuth recovery procedure in [GMAIL-API.md](GMAIL-API.md)):

```sh
sh prepare.sh
python3 mailagent.py restore /path/to/snapshot.sqlite3
python3 mailagent.py setup-telegram
python3 mailagent.py verify-telegram
python3 mailagent.py setup-gmail
python3 mailagent.py verify-gmail
python3 mailagent.py setup-model
python3 mailagent.py verify-model
sudo docker compose up -d --build
```

Expected: `RESTORE_OK` and preserved progress. Restore requires an empty data directory and the same Gmail account. A kernel lock prevents duplicate workers on one host; active multi-host operation is unsupported.

## Development

```sh
python -m unittest discover -s tests -v
sudo docker compose config --quiet
```

Tests cover crash recovery, API history synchronization, deduplication, snapshots, retries, tool feedback, webhook authentication, and deployment migration. Mock API responses are not evidence of live delivery latency.

References: [Telegram](https://core.telegram.org/bots/api#sendmessage), [Gmail IMAP](https://developers.google.com/workspace/gmail/imap/imap-extensions), [App passwords](https://support.google.com/mail/answer/185833), [Compose](https://docs.docker.com/reference/compose-file/services/).
