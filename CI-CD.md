# GitHub Actions

Repository: `Tommy-Chen-NZ/telegram-mail-test`.

The workflow runs on GitHub. Check the Actions page for the status of each revision. Deployment to cand5 requires the environment variables, SSH secrets, and server preparation below.

## Workflow

`.github/workflows/ci.yml` runs tests, builds React, validates host-network Compose settings, and builds the application image on GitHub-hosted Ubuntu runners.

- Pull request: checks and build only; no production secrets or deployment.
- Push to `main`: checks, then publishes AMD64 and ARM64 images to `ghcr.io/tommy-chen-nz/telegram-mail-test:sha-COMMIT`.
- Manual run on `main`, with `deploy` checked: checks and publishes that revision, then deploys the resulting immutable image digest to cand5 over SSH.
- Deployment uses the `cand5` environment and a concurrency lock. It does not run a self-hosted Actions runner on the 512 MiB VPS.

## One-time server preparation

Complete the initial manual deployment and service checks in [CAND5.md](CAND5.md) before enabling CD. Keep `data/`, `secrets/`, `.env`, and `compose.yaml` in one stable directory, for example `/home/USER/telegram-mail-test`.

The deployment user needs Python 3, Bash, `flock`, Docker Compose v2 supporting `up --wait`, and noninteractive access to Docker: either direct access or `sudo -n docker`. Docker access is effectively root access; use a dedicated deployment SSH key. Install its public key in the account's `authorized_keys`. The private key remains a GitHub environment secret.

Private GHCR images require a one-time registry login on cand5 under the same Docker account used by deployment. Use a GitHub token with `read:packages`, entered interactively:

```sh
read -r -p 'GitHub username: ' REGISTRY_USER
read -r -s -p 'Package read token: ' REGISTRY_TOKEN
printf '\n'
printf '%s' "$REGISTRY_TOKEN" | sudo docker login ghcr.io -u "$REGISTRY_USER" --password-stdin
unset REGISTRY_TOKEN
```

If the deployment user uses Docker without sudo, omit sudo when logging in. A public GHCR image can be pulled without this token. Application API keys and the SQLite database never go into GitHub.

## GitHub environment configuration

In repository Settings → Environments, create **cand5**. Restrict deployment branches to `main`. Optional reviewer protection depends on repository visibility and your GitHub plan.

Add these **environment secrets**:

| Name | Value |
| --- | --- |
| `SSH_PRIVATE_KEY` | Dedicated deployment key, without an interactive passphrase |
| `SSH_KNOWN_HOSTS` | Verified SSH host-key entry for cand5's externally reachable host and SSH port |

Verify the host-key fingerprint using a trusted server console or an existing trusted SSH connection. For a nonstandard port, the known_hosts entry uses `[HOST]:PORT`. The workflow enforces host-key checking; it does not blindly trust `ssh-keyscan` output.

Add these **environment variables**:

| Name | Value |
| --- | --- |
| `DEPLOY_HOST` | Public SSH hostname or IPv4 address reachable from GitHub runners |
| `DEPLOY_PORT` | External SSH port, default `22`; not the webhook port 8005 |
| `DEPLOY_USER` | SSH deployment account |
| `DEPLOY_DIR` | Absolute existing project directory, without spaces or `..` |
| `DEPLOY_PROJECT` | Existing Compose project name, default `telegram-mail-test` |

Find the existing project name with `sudo docker compose ls`. Keep it unchanged to avoid creating a second deployment against the same database and host ports.

Gmail, model, Telegram, and OAuth credentials stay in the server's `secrets/` directory. Do not add those credentials as Actions secrets.

## First run

Commit and push the prepared files to `main`. In Actions, open **Test, build and deploy**. Confirm the checks and image publication pass before configuring live deployment.

Then choose **Run workflow**, select `main`, and check `deploy`. Check `webhook` only after its server configuration is complete. A webhook already running in the same Compose project is retained even if the checkbox is off.

The server downloads the image instead of building it. Host networking, restart policies, persistent data, read-only credentials, dashboard loopback binding, and webhook port 8080 remain unchanged.

## Deployment and rollback

Each deployment:

1. Acquires a server-side lock and records the previous effective Compose configuration.
2. Creates a consistent SQLite snapshot under `backups/deploy-TIMESTAMP-COMMIT/`.
3. Pulls the tested image by digest and starts the selected services with `--no-build`.
4. Waits up to 180 seconds for health checks.
5. On success, writes `MAIL_AGENT_IMAGE` into `.env` and updates the root Compose file. Future manual Compose commands use that image.
6. On health failure, stops the failed release and attempts to restore the previous configuration for services that were running before the update.

Automatic rollback changes code only, never the live database. Restoring an older mail database could replay already-delivered notifications. Future incompatible schema changes need a separately reviewed migration/rollback plan. Old images and backups are retained; monitor disk space and archive backups off-host.

Expected success marker: `DEPLOY_OK`. A failed release exits unsuccessfully even if `ROLLBACK_OK` follows. If rollback also fails, inspect the services and the retained backup before another deployment.

Pipeline success does not prove 60-second delivery or reboot recovery on cand5. Those remain separate live tests.

References: [Publishing Docker images](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images), [Deployment environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments).
