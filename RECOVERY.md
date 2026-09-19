# Five-step recovery

This is a recovery procedure for a lost server, not just a container restart. A replacement-host restore and reboot test are still required before calling it verified.

## Off-host prerequisites

Keep a consistent SQLite snapshot, the complete `secrets/` directory (including `ngrok/`), `.env`, and the deployed image digest and matching full Git commit outside cand5. Store credentials and mail data in protected, preferably encrypted backup storage. Never commit them to GitHub. A backup kept only on cand5 cannot recover a lost box.

Create a live SQLite snapshot with the application's backup API rather than copying an active database/WAL file:

```sh
cd ~/telegram-mail-test
python3 mailagent.py backup "backups/agent-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
```

Transfer the snapshot and configuration securely off-host. This command does not perform that transfer. Recovery loses changes made after the selected snapshot and may resend messages sent after that snapshot. Confirm the old server is stopped before running its replacement.

## 1. Prepare the replacement host

Install Docker, Compose v2, Python 3 and Git on Ubuntu 24.04; enable Docker at boot. Use the same deployment account throughout. Keep host networking, including for temporary containers, on the nested LXC environment. Ensure you can access the private GitHub repository and GHCR image; these are separate permissions. Enter credentials at secure authentication prompts, never in repository URLs or shell arguments.

```sh
sudo systemctl enable --now docker
sudo docker login ghcr.io -u Tommy-Chen-NZ
```

## 2. Fetch the matching release from GitHub

Replace `RECOVERY_COMMIT` below with the full commit recorded with the backup, whose image was successfully published. Use new, empty directories. Keeping source and runtime directories separate prevents Git updates from conflicting with deployment-managed files.

```sh
git clone https://github.com/Tommy-Chen-NZ/telegram-mail-test.git ~/telegram-mail-source
git -C ~/telegram-mail-source checkout --detach RECOVERY_COMMIT
mkdir ~/telegram-mail-test
git -C ~/telegram-mail-source archive HEAD | tar -xf - -C ~/telegram-mail-test
cd ~/telegram-mail-test
```

## 3. Restore state and credentials before starting services

Securely restore the saved `.env` and complete `secrets/` directory into `~/telegram-mail-test`. Keep the snapshot outside `data/`; replace its example path below. Do not run setup, status or verification commands that create an empty database before restoring it.

```sh
sh prepare.sh
python3 mailagent.py restore /secure-restore/agent.sqlite3
find secrets -type d -exec chmod 700 {} +
find secrets -type f -exec chmod 600 {} +
sudo docker compose --profile webhook pull agent webhook
```

Ensure restored files belong to the deployment account; `prepare.sh` sets Compose UID/GID for that account while preserving the image and webhook settings. Expect `RESTORE_OK`. The restored SQLite includes mailbox cursors, deduplication records, retries, cross-email memory and an imported prompt. The saved `.env` selects the pinned image; do not build a new image on the small server.

## 4. Verify access and start the services

If credentials are expired or revoked, renew them through the existing hidden setup prompts before continuing. Keep the same Gmail account and ngrok domain. Existing Google Cloud resources can be reused if their configuration is unchanged.

```sh
sudo docker compose run --rm --no-deps agent verify-telegram
sudo docker compose run --rm --no-deps agent verify-gmail
sudo docker compose run --rm --no-deps agent verify-model
```

Continue only after verification succeeds and the Telegram test message is visible:

```sh
sudo docker compose --profile webhook up -d --no-build --wait webhook
sudo docker compose -p telegram-mail-tunnel -f compose.ngrok.yaml pull
sudo docker compose -p telegram-mail-tunnel -f compose.ngrok.yaml up -d --no-build
python3 ngrok_setup.py verify
sudo docker compose run --rm --no-deps webhook watch
sudo docker compose up -d --no-build --wait agent
```

Expect tunnel verification, `GMAIL_WATCH_OK` and healthy application services. If the public domain changes, update the Pub/Sub push endpoint and OIDC audience together with the receiver configuration.

## 5. Verify delivery and reboot recovery

Send a uniquely identified email and confirm its Telegram summary and measured latency. Inspect `status` and `memory`, then reboot. Without manually starting containers, confirm another email is delivered after the host comes back. Check the separate ngrok project too.

```sh
sudo docker compose exec -T agent python /app/mailagent.py status
sudo docker compose exec -T agent python /app/mailagent.py memory
sudo docker compose -p telegram-mail-tunnel -f compose.ngrok.yaml ps
sudo docker stats --no-stream
```

## Five-line handover version

1. Prepare Ubuntu 24.04 with Docker Compose, enable Docker at boot, and ensure the old server is stopped.
2. Clone the private GitHub repository at the recorded release commit and pull its matching pinned GHCR image.
3. Restore the off-host SQLite snapshot, credentials and environment configuration; correct ownership and permissions.
4. Verify Telegram, Gmail and model access; start the webhook and ngrok, renew Gmail watch, then start the agent.
5. Send a test email, confirm its Telegram summary and latency, and reboot to verify unattended recovery.
