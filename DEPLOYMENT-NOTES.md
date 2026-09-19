# Deployment notes

Draft for the test handover, based on observed server output and user confirmation.

## What broke along the way

- **Docker networking:** The default Docker network failed with `sysctl permission denied` inside the nested LXC environment. Using host networking allowed containers to start; Compose uses `network_mode: host` without port mappings.
- **Model availability:** The initially configured Gemini model appeared in the model list but returned HTTP 404 when called. Switching to an available model restored the real `read_mail` -> `submit_summary` tool loop.
- **Gmail authorization:** Google blocked OAuth access while the app was in Testing because the mailbox owner was not an approved tester. Adding the account as a test user allowed authorization to complete.
- **Pub/Sub delivery:** Push requests initially returned HTTP 400 and messages accumulated without acknowledgement. We added safe request diagnostics and support for numeric history IDs. After deployment, authenticated pushes returned HTTP 204. The original failing payload was not captured, so the precise cause remains unconfirmed.

## Evidence collected

- Gmail API mode fetched a new email and recorded a successful Telegram send in 24.14 seconds, including 2.97 seconds from discovery to send completion.
- A separate uniquely labelled Telegram delivery check returned the expected private chat ID, and the recipient confirmed seeing it.
- Authenticated Pub/Sub requests were accepted; unsigned webhook requests were rejected with HTTP 401.

## Still to verify before final handover

- Email delivery while SSH is disconnected and automatic recovery after a full server reboot.
- Off-host backup and restoration on a replacement server, including SQLite and securely stored credentials.
- Actual memory usage and repeated end-to-end latency measurements.
- Long-running OAuth access: the app was configured in Testing; production configuration and renewed authorization have not yet been confirmed.

The final submission should include a Telegram screenshot and a five-line recovery runbook validated by a restore test.
