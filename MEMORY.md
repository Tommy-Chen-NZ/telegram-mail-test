# Cross-email memory

New emails share one compact English memory for this mailbox. The model receives the previous memory as untrusted context, reads the current email through `read_mail`, and returns both the current summary and a full replacement memory through `submit_summary`. No additional model request or service is needed for the update.

The prompt retains attributed contacts, ongoing projects, open actions and explicit deadlines. It asks the model to remove resolved or superseded facts, avoid invented preferences, and exclude credentials and instructions directed at the agent. These are model instructions, not a guarantee that the model cannot make mistakes. Memory is never executed and grants no extra tools or permissions.

## Size and persistence

- Target: below 500 tokens. The enforced limit is **1500 UTF-8 bytes**; the exact token count varies by model and is not measured by a tokenizer.
- The model writes English memory. This is one rolling note, not a vector database or archive of every email.
- SQLite stores memory and its source job/time atomically with the generated summary, before Telegram delivery. It therefore describes processed mail, including a message whose Telegram delivery may still be retrying.
- Delivery retries reuse the saved summary and cannot replay an old memory update over newer context. Missing, invalid or oversized updates retain the previous memory while allowing a valid summary to be delivered.
- Memory survives restart and is included in SQLite backups. No old emails are replayed to populate it automatically. Existing jobs with saved summaries do not regenerate memory.
- Normal status and logs expose only memory size, source job, timestamp and event codes. Reading memory explicitly may reveal private email context. That context is also sent to the configured model API when processing later emails.

## Inspect or clear

From the deployment directory:

```sh
sudo docker compose exec -T agent python /app/mailagent.py memory
```

Clear while the worker is stopped, then restart it. This does not delete delivery records or change mailbox progress. Existing backups still contain their earlier memory snapshot.

```sh
sudo docker compose stop agent
sudo docker compose run --rm --no-deps agent clear-memory
sudo docker compose up -d --no-build agent
```

## Live acceptance test

1. Run `sudo docker compose run --rm --no-deps agent verify-model`. Expect `MODEL_TOOL_LOOP_OK` and `MEMORY_OUTPUT_OK`; this synthetic check does not change persistent memory.
2. Send an email stating: "Project Atlas: the draft is due September 25, 2026. Alex is the contact." Wait for its Telegram summary, then inspect memory.
3. Send another email stating: "Project Atlas update: the draft deadline has moved to September 28, 2026. Alex remains the contact." Wait for delivery and inspect memory again. The new deadline should replace the old one.
4. Restart the agent and inspect memory again; it should be unchanged. Check status for delivery latency and `memory_updated` events.

Local tests exercise context transfer, persistence, byte limits, invalid updates, atomic rollback, backup retention and retry ordering with simulated model responses. Actual Gemini behavior and latency with memory still require the live acceptance test.
