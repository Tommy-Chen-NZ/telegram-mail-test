# cand5 deployment checks

Target: LXC nested inside KVM. Docker Compose remains the deployment method.

| Check | Current evidence |
| --- | --- |
| Basic container with host networking | Passed, reported by user: `docker run --rm --network host hello-world` |
| Application image build | Not yet tested on cand5 |
| Application startup | Healthy containers reported by user |
| Resource limits | Actual usage still needs measurement |
| Container → Telegram | Not yet tested on cand5 |
| Container → Gmail | Not yet tested on cand5 |
| Container → model API | Not yet tested on cand5 |
| Public webhook and signed Pub/Sub delivery | Authenticated HTTP 204 observed through ngrok |
| Delivery within 60 seconds | One API-mode delivery recorded at 24.14 seconds; repeat after restart |
| SSH disconnect and server reboot recovery | Not yet tested on cand5 |

The locally validated Compose configuration requests host networking for builds and both application services, has no `ports` entries, and retains `restart: unless-stopped`. The shared network is **cand5's**, not the outer KVM host's. No privileged mode, extra capabilities, or sysctl overrides are added.

## 1. Build

Inside the project directory on cand5:

```sh
sh prepare.sh
sudo docker compose --profile webhook config --quiet
sudo docker compose build agent
```

Success: exit code 0 and the `mail-summary-agent:local` image exists. All services use this same image. Build it once to avoid unnecessary work on the 512 MiB machine. Build-time host networking is separate from runtime `network_mode`; if the builder rejects the `network.host` entitlement or another LXC restriction, record that error as a build failure, not an application networking result.

## 2. Configure and test from containers

Enter credentials locally at hidden prompts; run verification inside Compose containers:

```sh
python3 mailagent.py setup-telegram
sudo docker compose run --rm --no-deps agent verify-telegram

python3 mailagent.py setup-gmail
sudo docker compose run --rm --no-deps agent verify-gmail

python3 mailagent.py setup-model
sudo docker compose run --rm --no-deps agent verify-model
```

Success markers, in order: `TELEGRAM_SEND_OK` plus the Telegram test message, `GMAIL_OK`, and `MODEL_TOOL_LOOP_OK`. A host-side Python test alone is not proof that the application container can access a service.

## 3. Start

```sh
sudo docker compose up -d --no-build
sudo docker compose ps
sudo docker compose exec -T agent python /app/mailagent.py health
sudo docker stats --no-stream
```

Success: running services become healthy and the worker returns `HEALTH_OK`. The agent has no inbound listener. Configure the webhook separately using [WEBHOOK.md](WEBHOOK.md); it listens on cand5 port 8080 for your external 8005 → 8080 forward.

All containers retain the same `./data:/data` bind mount for SQLite and `./secrets:/run/agent-secrets:ro` for credentials. `prepare.sh` sets directory permissions and runtime UID/GID while preserving other `.env` entries, including the deployed image and webhook bind address. Run it as the same account that owns the deployment files.

## 4. Delivery and recovery

1. Send three uniquely numbered emails, approximately 30 seconds apart. Match each Telegram message ID to the persisted status and confirm `gmail_to_telegram_seconds <= 60`.
2. Disconnect SSH, send another email, and confirm delivery continues.
3. Save a status snapshot, then reboot cand5. Reconnect without running `compose up`. Verify automatic service startup, preserved Telegram IDs, and processing of mail received during the outage.
4. Send a new post-reboot email and measure latency again. Outage time is excluded from the normal-operation latency target.

```sh
sudo docker compose exec -T agent python /app/mailagent.py status
sudo systemctl is-enabled docker
```

Enable Docker at boot with `sudo systemctl enable docker` if required. `unless-stopped` does not reactivate a manually stopped service. Keep the complete project `data` directory across container recreation; use the documented SQLite backup command for off-host copies.

For an existing installation with working Pub/Sub and OAuth, follow [GMAIL-API.md](GMAIL-API.md) to switch the mail reader before the final latency and reboot tests. The switch preserves the original checkpoint and delivery records.

Do not mark the deployment verified until these cand5 tests have actual results. Local mocked tests and Compose validation cannot establish server connectivity, memory usage, latency, or reboot recovery.
