# Server-pull deployment

GitHub Actions tests the application and publishes AMD64/ARM64 images. The server pulls a selected release; GitHub never connects to cand5. No SSH secrets, deployment environment variables, or self-hosted runner are needed. The old cand5 environment can remain unused.

Push to main or run **Test and publish** manually. Wait for both checks and publish to pass. Use the exact image digest from that run's summary (preferred), or its full `sha-COMMIT` tag. There is no automatic latest-version rollout.

## 1. Registry login

Create a GitHub personal access token **(classic)** with only `read:packages` and an expiry date. Enter it at the Docker password prompt, not in a command, source file, or chat. It grants access to images, not private Git source.

On cand5, using the account that will own the project:

```sh
if docker info >/dev/null 2>&1; then
  docker login ghcr.io -u Tommy-Chen-NZ
else
  sudo docker login ghcr.io -u Tommy-Chen-NZ
fi
```

Password means the token, not the GitHub account password. Expected: `Login Succeeded`. Docker retains the registry credential in that Docker account's configuration; keep it protected and renew the token before expiry. Application credentials remain in `secrets/` on cand5.

## 2. First installation

Use a new directory. For an existing installation, preserve its data, secrets, .env and Compose project name and use the update procedure instead. Ubuntu needs Python 3, Bash, tar, flock and Docker Compose v2 with `up --wait`. No Node or Git is needed on the server.

Set IMAGE to the complete digest shown by the successful Actions run, then extract its deployment bundle. Run this in Bash; replace the example image before running:

```sh
IMAGE='ghcr.io/tommy-chen-nz/telegram-mail-test@sha256:REPLACE_WITH_DIGEST'
mkdir -p ~/telegram-mail-test
cd ~/telegram-mail-test
if docker info >/dev/null 2>&1; then engine=(docker); else engine=(sudo docker); fi
"${engine[@]}" pull "$IMAGE"
container=$("${engine[@]}" create --network host "$IMAGE")
"${engine[@]}" cp "$container:/opt/deployment/." - | tar --no-same-owner -xf -
"${engine[@]}" rm "$container"
bash scripts/pull-release.sh "$IMAGE" prepare
```

Expected: `PULL_READY`. The temporary container is never started. The script pins the downloaded digest in .env and prepares data, secrets and backups with private permissions. No application services start yet. An existing .env prevents preparation from being run twice.

## 3. Verify Telegram first

In that same directory:

```sh
python3 mailagent.py setup-telegram
sudo docker compose run --rm --no-deps agent verify-telegram
```

Expected: `TELEGRAM_SEND_OK` and an actual test message in Telegram. Enter the bot token only at the hidden setup prompt. Continue with Gmail and model setup in [CAND5.md](CAND5.md), skipping its local build step because the image is already downloaded. Start the services only after credential verification succeeds. Gmail Pub/Sub setup is documented in [WEBHOOK.md](WEBHOOK.md).

`docker compose run` does not accept `--no-build`; use that flag only with supported commands such as `up`. If Gmail setup reports `verify_telegram_first`, rerun the Telegram verification above from the project directory and confirm `TELEGRAM_SEND_OK` before continuing. Saving a bot token alone does not record a successful verification.

```sh
bash scripts/pull-release.sh "$IMAGE" deploy
```

Expected: `DEPLOY_OK` and healthy services. Use `deploy true` only after configuring the webhook. All services use host networking, no ports mapping, persistent SQLite, read-only mounted credentials, and restart unless-stopped. The webhook uses port 8080; there is no management frontend.

## 4. Subsequent updates

Wait for the desired Actions run to pass, copy its image digest, then run on cand5:

```sh
cd ~/telegram-mail-test
bash scripts/pull-release.sh 'ghcr.io/tommy-chen-nz/telegram-mail-test@sha256:REPLACE_WITH_DIGEST' deploy
```

The script extracts the Compose file and deployment tools from that exact image, snapshots SQLite, updates services without building, and waits for health checks. It keeps an existing running webhook enabled. For a nondefault existing Compose project, use `deploy false EXISTING_PROJECT` (or `deploy true EXISTING_PROJECT` to enable the webhook).

On a failed rollout it attempts to restore the previous code and running services, without restoring an older database that could replay notifications. Look for `ROLLBACK_OK` or `ROLLBACK_FAILED`; either still means the update failed. Old images and backups are retained. Monitor disk space and copy SQLite snapshots off-host. Incompatible future schema changes require a migration plan.

Do not extract a bundle directly over an existing deployment; use pull-release.sh so rollback retains the previous Compose configuration. After updating, the terminal setup tools are refreshed alongside the chosen application release.

Pipeline success does not verify cand5 network access, its memory limits, 60-second delivery or reboot recovery. Complete the live tests in [CAND5.md](CAND5.md). Updates are manual for now; no timer is installed.

Reference: [GitHub Container registry authentication](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

On upgrade from a release with a dashboard, a healthy rollout removes only containers labelled as the dashboard service in the selected Compose project. SQLite, credentials, ngrok, and unrelated containers are preserved. A failed rollout retains the old dashboard for rollback. Two inert dashboard command notices remain in the image deployment bundle solely for compatibility with previously installed pull scripts; the application image has no frontend or dashboard server.
