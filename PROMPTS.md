# Change the summary prompt through GitHub

Edit `prompts/summary.txt` in GitHub and commit it to `main`. Keep instructions in English and the UTF-8 file at most 8192 bytes. Do not include credentials or private email content. The file is version-controlled; the prompt is applied only when explicitly imported on the server.

For the first use, clone a separate source checkout. The existing runtime directory may have been installed from an image bundle and is not necessarily a Git repository:

```sh
git clone https://github.com/Tommy-Chen-NZ/telegram-mail-test.git ~/telegram-mail-source
```

Authenticate privately when prompted. Do not put a GitHub token in the clone URL. If using a source checkout from the recovery procedure, switch it back to `main` before pulling:

```sh
git -C ~/telegram-mail-source switch main
git -C ~/telegram-mail-source pull --ff-only origin main
```

The running release must support `set-prompt`. Deploy the first release containing this command through the normal image-pull process before importing. Later prompt-only edits do not require a new image or a local build.

```sh
cd ~/telegram-mail-test
sudo docker compose stop agent
python3 mailagent.py set-prompt ~/telegram-mail-source/prompts/summary.txt
sudo docker compose run --rm --no-deps agent verify-model
```

After `PROMPT_SAVED`, `MODEL_TOOL_LOOP_OK` and `MEMORY_OUTPUT_OK`, restart:

```sh
sudo docker compose up -d --no-build agent
```

Do not restart after a failed import or verification; correct the prompt or restore its previous Git revision and repeat. Webhook ingestion and ngrok remain running while the worker is stopped. Received notifications remain queued in SQLite.

The import preserves the selected model, endpoint, API credential file, mailbox progress and cross-email memory. It changes only the summary preferences stored in SQLite and requires model verification before restart. Newly generated summaries use the new prompt; existing summaries waiting for Telegram retries are not rewritten. Active prompt settings are included in SQLite backups. Rolling back the image alone does not roll back the imported prompt; reimport the desired file version.

The summary file does not override the fixed tool permissions, memory size limit or email-safety instructions. Memory-update rules still live in `MEMORY_PROMPT` in `mailagent.py`; changing those requires a tested image release. A Git pull alone neither imports a prompt nor deploys new application code.
