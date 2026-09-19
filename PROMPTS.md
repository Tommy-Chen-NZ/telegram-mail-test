# Configure the summary prompt

Edit the existing `SUMMARY_PROMPT` entry in the server's `.env`. Preserve all credentials and deployment settings:

```dotenv
SUMMARY_PROMPT='Summarize the current email in English in fewer than 100 words. Include the main point, action items and explicit deadlines. Use prior memory only when relevant.'
```

Use one physical line, single quotes and at most 8192 UTF-8 bytes. Dollar signs and hash characters remain literal. Escape an apostrophe inside the value as `\'`. The literal file reader does not expand shell commands or `${VARIABLE}` references. For escaped newlines, a JSON-style double-quoted value without dollar signs is supported; actual multiline entries are rejected. See [CONFIGURATION.md](CONFIGURATION.md) for unified credentials and migration.

From the deployment directory, stop the worker and verify the new prompt in a fresh container:

```sh
cd ~/telegram-mail-test
sudo docker compose stop agent
sudo docker compose run --rm --no-deps agent verify-model
```

After `MODEL_TOOL_LOOP_OK` and `MEMORY_OUTPUT_OK`, recreate the worker:

```sh
sudo docker compose up -d --no-build --force-recreate agent
```

On failure, correct the value or restore the previous one, verify again and then recreate. Webhook and ngrok can stay running during a prompt-only change. Recreating is required because editors may replace the file's inode; do not rely on `restart` to pick up a changed bind-mounted file.

The application reads `.env` directly as a read-only mounted file, not through credential environment variables. Host-side setup commands read the local `.env` too. A nonblank file `SUMMARY_PROMPT` takes priority over an imported SQLite prompt and the credential-file/default prompt. Blank or absent values restore the existing fallback, which may be a previously imported prompt. The environment override does not overwrite SQLite settings or memory. Include `.env` in the off-host backup; a SQLite snapshot alone does not contain it.

## Optional: import a prompt from GitHub

Edit `prompts/summary.txt` in GitHub and commit it. Remove or blank `SUMMARY_PROMPT` in `.env` to let the imported prompt take effect. On the first use, clone into a separate source directory:

```sh
git clone https://github.com/Tommy-Chen-NZ/telegram-mail-test.git ~/telegram-mail-source
```

Then pull and import without rebuilding the image:

```sh
git -C ~/telegram-mail-source switch main
git -C ~/telegram-mail-source pull --ff-only origin main
cd ~/telegram-mail-test
sudo docker compose stop agent
python3 mailagent.py set-prompt ~/telegram-mail-source/prompts/summary.txt
sudo docker compose run --rm --no-deps agent verify-model
```

After verification succeeds, recreate the worker using the command above. The import preserves the selected model, API credentials, mail progress and memory. Existing summaries waiting for Telegram retries are not rewritten. Imported settings are backed up in SQLite; an image rollback alone does not revert them.

Neither configuration method overrides fixed tool permissions or memory size limits. The memory-update rules remain in `MEMORY_PROMPT` in the application and require an image release to change. A Git pull alone neither imports a prompt nor deploys application code.
