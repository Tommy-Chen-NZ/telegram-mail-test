# One private .env for application configuration

The server's `.env` can hold Telegram, Gmail OAuth, optional legacy IMAP, model API, ngrok credentials, webhook configuration and the summary prompt. The application reads it as a private, read-only file. Credentials are not injected into Docker environment variables or expanded into `docker compose config` output. Do not use `env_file: .env`, publish `.env`, print it in diagnostics, or paste it into chat. The tracked `.env.example` contains empty credential fields only.

## Migrate an existing installation

First deploy the successfully published release containing `migrate-env`. Do not replace `.env` with the example. The upgrade initially continues to read the existing JSON credential files.

```sh
cd ~/telegram-mail-test
sudo docker compose stop agent webhook
python3 mailagent.py migrate-env
python3 ngrok_setup.py render
```

Expect `ENV_MIGRATED` and `NGROK_CONFIG_RENDERED`. Migration copies existing values without printing them, preserves infrastructure settings, exports the effective model selection and prompt, and sets `.env` mode 0600. Conflicting manually entered credentials abort migration without rewriting the file. This migration expects the Gmail API/PubSub/ngrok installation to be configured; it is not an IMAP-only setup wizard.

Reverify before restarting:

```sh
sudo docker compose run --rm --no-deps agent verify-telegram
sudo docker compose run --rm --no-deps agent verify-gmail
sudo docker compose run --rm --no-deps agent verify-model
```

Continue only after the Telegram test is visible and all checks succeed:

```sh
sudo docker compose --profile webhook up -d --no-build --force-recreate --wait agent webhook
sudo docker compose -p telegram-mail-tunnel -f compose.ngrok.yaml up -d --no-build --force-recreate
python3 ngrok_setup.py verify
sudo docker compose run --rm --no-deps webhook watch
```

Send a real email and inspect status. The old JSON files are deliberately retained for rollback and can be securely removed later after live validation. With `ENV_CONFIG_VERSION='1'`, the application does not fall back to them if a dotenv credential is missing. Existing hidden setup commands now save into `.env`; use them on the host, since containers mount it read-only. Do not rerun migration after intentionally changing credentials: edit `.env` or use setup instead.

## Fields

| Purpose | Keys |
| --- | --- |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| Gmail mailbox | `GMAIL_ADDRESS`; optional `GMAIL_APP_PASSWORD` for legacy IMAP only |
| Gmail OAuth | `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` |
| Pub/Sub | `GMAIL_PUBSUB_TOPIC`, `GMAIL_PUBSUB_SUBSCRIPTION`, `GMAIL_PUSH_SERVICE_ACCOUNT`, `GMAIL_WEBHOOK_AUDIENCE` |
| Model | `MODEL_API_URL`, `MODEL_NAME`, `MODEL_API_KEY`, `SUMMARY_PROMPT` |
| ngrok | `NGROK_DOMAIN`, `NGROK_AUTHTOKEN` |
| Deployment | `ENV_CONFIG_VERSION`, `MAIL_AGENT_IMAGE`, `COMPOSE_PROJECT_NAME`, `LOCAL_UID`, `LOCAL_GID`, `WEBHOOK_BIND` |

`MODEL_BASE_PROMPT` may also be present after migration to preserve an older credential-file prompt. Usually edit `SUMMARY_PROMPT` instead. OAuth access tokens are short-lived and remain in memory; they do not need backing up. A GHCR login credential is deployment infrastructure, not an application secret: authenticate Docker separately on a replacement host rather than adding it to this file.

Use one assignment per physical line, single-quoted literal secret values, no duplicate keys and no variable expansion. The reader accepts comments, plain values and JSON-style double-quoted strings. Use the one-line examples for straightforward editing. Never source this file as a shell script.

After credential changes, stop affected services, verify in fresh Compose containers and recreate agent/webhook. After ngrok token/domain changes, run `python3 ngrok_setup.py render` and recreate the tunnel too. A changed domain also requires corresponding Pub/Sub endpoint/audience changes. Cached access tokens and bind mounts make a simple process restart insufficient as a general reload procedure.

## Backup and recovery

After migration, the essential off-host application backup is **the private `.env` plus a consistent SQLite snapshot**, together with the recorded code/image release. The ngrok configuration under `secrets/ngrok/config.yml` is a generated, private file containing only the ngrok credential; recreate it with `ngrok_setup.py render`. SQLite carries progress, deduplication, retries and cross-email memory. Keep the image available in GHCR or archive it separately if recovery must work without registry access. See [RECOVERY.md](RECOVERY.md).

Automated tests cover migration, environment-only credentials, safe failure on missing fields, prompt precedence, Gmail watch renewal without JSON files, ngrok regeneration and absence of secrets from Compose diagnostics. Actual cand5 migration, restart and off-host restore remain live acceptance checks.
