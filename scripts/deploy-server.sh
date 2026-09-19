#!/usr/bin/env bash
set -euo pipefail
umask 077
root=${1:?Deployment directory required}
revision=${2:?Revision required}
image=${3:?Image required}
webhook=${4:-false}
project=${5:-telegram-mail-test}
[[ $root = /* && $root != / && $root != *'..'* && $revision =~ ^[a-f0-9]{40}$ ]] || exit 1
[[ $image =~ ^ghcr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$ ]] || exit 1
[[ $webhook == true || $webhook == false ]] || exit 1
[[ $project =~ ^[a-z0-9][a-z0-9_-]*$ ]] || exit 1
cd "$root"
[[ -f .env && -d data && -d secrets && -f compose.yaml ]] || {
  echo 'Bootstrap the deployment directory and credentials before enabling CD.'; exit 1;
}
exec 9> .deploy.lock
flock -n 9 || { echo 'Another deployment is running.'; exit 1; }

# Use the existing Docker login of this account (or root when using sudo).
if docker info >/dev/null 2>&1; then
  engine=(docker)
else
  engine=(sudo -n docker)
  "${engine[@]}" info >/dev/null
fi
services=(agent)
previous_services=()
for service in agent dashboard webhook; do
  running=$("${engine[@]}" ps -q --filter "label=com.docker.compose.project=$project" --filter "label=com.docker.compose.service=$service")
  if [[ -n $running ]]; then previous_services+=("$service"); fi
done
existing_webhook=$("${engine[@]}" ps -q --filter "label=com.docker.compose.project=$project" --filter label=com.docker.compose.service=webhook)
if [[ $webhook == true || -n $existing_webhook ]]; then services+=(webhook); fi

release="$root/releases/$revision"
[[ -f "$release/compose.yaml" ]] || exit 1
backup="$root/backups/deploy-$(date -u +%Y%m%dT%H%M%SZ)-$revision"
mkdir -p "$backup"
"${engine[@]}" compose --project-directory "$root" --env-file "$root/.env" -p "$project" \
  -f "$root/compose.yaml" --profile webhook config --format json > "$backup/compose.json"
cp .env "$backup/env"
# Snapshot SQLite before running any code from the new image. Preserve database/WAL in place.
python3 - "$root/data/agent.sqlite3" "$backup/agent.sqlite3" <<'PY'
from pathlib import Path
import sqlite3, sys
source, target = map(Path, sys.argv[1:])
if source.exists():
    with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as db:
        with sqlite3.connect(target) as snapshot:
            db.backup(snapshot)
            assert snapshot.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    print('DEPLOY_BACKUP_OK')
PY

"${engine[@]}" pull "$image"
printf 'MAIL_AGENT_IMAGE=%s\n' "$image" > "$release/image.env"
unset MAIL_AGENT_IMAGE
compose_new() {
  "${engine[@]}" compose --project-directory "$root" --env-file "$root/.env" --env-file "$release/image.env" \
    -p "$project" -f "$release/compose.yaml" --profile webhook "$@"
}
compose_new config --quiet
if ! compose_new up -d --no-build --pull never --wait --wait-timeout 180 "${services[@]}"; then
  echo 'DEPLOY_FAILED: attempting to restore the previous Compose configuration.'
  # Roll back code only. Never automatically restore an older mail database:
  # doing so could replay notifications already delivered to Telegram.
  compose_new stop "${services[@]}" || true
  if [[ ${#previous_services[@]} -eq 0 ]]; then
    echo 'No previous running services. Failed services have been stopped.'
  elif "${engine[@]}" compose --project-directory "$root" -p "$project" -f "$backup/compose.json" \
      --profile webhook up -d --no-build --pull never --wait --wait-timeout 180 "${previous_services[@]}"; then
    echo 'ROLLBACK_OK: database kept at its current state.'
  else
    echo "ROLLBACK_FAILED: inspect services and backup at $backup"
  fi
  exit 1
fi

# Persist the image selection for subsequent manual Compose commands.
python3 - "$root/.env" "$image" <<'PY'
from pathlib import Path
import os, sys
path, image = Path(sys.argv[1]), sys.argv[2]
lines = [line for line in path.read_text().splitlines() if not line.startswith('MAIL_AGENT_IMAGE=')]
temporary = path.with_name('.env.deploy.tmp')
temporary.write_text('\n'.join(lines + ['MAIL_AGENT_IMAGE=' + image]) + '\n')
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PY
cp "$release/compose.yaml" "$root/compose.yaml"
for helper in compose.ngrok.yaml ngrok_setup.py gmail_api.py; do
  if [[ -f "$release/$helper" ]]; then cp "$release/$helper" "$root/$helper"; fi
done
printf '%s\n' "$image" > "$root/deployed-image.txt"
# Retire only this project's UI after a healthy rollout. Keep data and secrets.
retired_dashboard=$("${engine[@]}" ps -aq --filter "label=com.docker.compose.project=$project" --filter label=com.docker.compose.service=dashboard)
if [[ -n $retired_dashboard ]]; then
  mapfile -t retired_ids <<< "$retired_dashboard"
  "${engine[@]}" rm --force "${retired_ids[@]}"
  echo 'DASHBOARD_REMOVED'
fi
echo 'DEPLOY_OK'
"${engine[@]}" compose --project-directory "$root" --env-file "$root/.env" -p "$project" \
  -f "$root/compose.yaml" --profile webhook ps
