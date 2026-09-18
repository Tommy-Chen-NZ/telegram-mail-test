# Gmail API reader

After Pub/Sub delivery is verified, switch the existing IMAP installation to OAuth-only mail reading. The existing `gmail_oauth.json` credentials are reused. An app password is not used in API mode. `gmail.json` is still needed for the expected email address.

## Switch an existing installation

Deploy a successful image through [CI-CD.md](CI-CD.md) first. Image deployment alone leaves the current reader selected. From the prepared directory on cand5:

```sh
sudo docker compose stop agent
sudo docker compose run --rm --no-deps agent enable-gmail-api
sudo docker compose up -d --no-build --wait --wait-timeout 180 agent
sudo docker compose exec -T agent python /app/mailagent.py status
```

Run the start command after `GMAIL_API_ENABLED`. The switch verifies the OAuth mailbox and reads one available INBOX message without queuing or printing it. It requires the existing Telegram verification and acquires the worker lock. A failed verification leaves the previous reader selected; start the agent again to resume it. A repeat switch preserves existing API progress.

Expected status: `gmail_source: gmail_api`, a populated `gmail_history_cursor` after synchronization, `gmail_fetch_pending: 0`, and fresh collector/worker heartbeats. Existing Telegram/model verification and sent-message records are retained. The receiver and ngrok remain online during the switch.

Send a new email with a unique subject. Confirm a `gmail_webhook_accepted` receiver log, a `discovered` event with `code: gmail_api`, and a new `sent` record with `gmail_to_telegram_seconds <= 60`. Then repeat after disconnecting SSH. Container restart and cand5 reboot are separate live tests in [CAND5.md](CAND5.md); an HTTP 204 alone does not establish end-to-end latency or reboot recovery.

After a successful API delivery, the old Gmail app password may be revoked. The reader, watch renewal, and webhook only need the expected email address from `gmail.json`; the `app_password` field is ignored in API mode. Keep OAuth credentials available in the existing read-only secret mount.

## Progress and resource limits

- Pub/Sub wakes the collector in approximately one second while idle. A 60-second Gmail history check is retained for missed notifications. Delayed pushes, retries, or backlogs can exceed the 60-second delivery target.
- Migration and expired-history recovery scan INBOX messages since the original installation checkpoint, 50 IDs per page. Older mail, spam, archived messages, and messages deleted before retrieval are excluded. Each scan captures a history baseline before listing, then replays changes made during the scan.
- API hexadecimal message IDs are converted to the existing decimal IMAP IDs, preserving deduplication across migration.
- History pages and the `gmail_fetch` queue commit together in SQLite. Body retrieval then commits the summary job and removes its fetch entry together. Network failures, restarts, and backups preserve pending fetches. The collector never advances its cursor from a webhook or watch-renewal history ID.
- API responses are limited to 512 KiB, body processing to 256 KiB and 20,000 text characters. At most ten messages are fetched per collector pass with a scheduling budget. Attachments are not fetched. Oversized responses fall back to a metadata snippet with an explicit truncation note.
- Access tokens stay in memory, refresh automatically, and are checked against the expected mailbox. No new SDK, container, or memory allocation limit is required.

## Credentials and recovery

When OAuth credentials change, run `python3 webhook.py setup-watch` with the new credentials, then `sudo docker compose run --rm --no-deps agent verify-gmail`. In API mode this verifies OAuth and preserves sync progress. Restart the agent if it stopped because verification was missing. An External OAuth app left in Testing may issue a refresh token that expires after seven days; for a long-running personal installation, move it to Production and authorize again as appropriate.

SQLite backups include the selected reader, history cursor, resync pagination, queued fetches, summary jobs, retries, and deduplication records. Restore the backup to an empty data directory on the replacement host, securely provision matching Gmail/OAuth/model/Telegram credentials, and reverify before starting. Keep the old host stopped to avoid duplicate sends. Never reset the database to switch readers.

References: [Gmail synchronization](https://developers.google.com/workspace/gmail/api/guides/sync), [history pagination and expiration](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list), [IMAP/API ID equivalence](https://developers.google.com/workspace/gmail/imap/imap-extensions), [OAuth token expiration](https://developers.google.com/identity/protocols/oauth2).
