#!/usr/bin/env bash
set -euo pipefail
umask 077
image=${1:?Usage: bash scripts/pull-release.sh IMAGE prepare|deploy [true|false] [PROJECT]}
mode=${2:?Choose prepare or deploy}
webhook=${3:-false}
project=${4:-telegram-mail-test}
repository=ghcr.io/tommy-chen-nz/telegram-mail-test
[[ $image =~ ^ghcr\.io/tommy-chen-nz/telegram-mail-test(:sha-[a-f0-9]{40}|@sha256:[a-f0-9]{64})$ ]] || {
  echo 'Use a full commit tag or digest from a successful Actions run.'; exit 1;
}
[[ $mode == prepare || $mode == deploy ]] || exit 1
[[ $webhook == true || $webhook == false ]] || exit 1
[[ $project =~ ^[a-z0-9][a-z0-9_-]*$ ]] || exit 1
root=$(cd "$(dirname "$0")/.." && pwd -P)
cd "$root"
exec 8> .pull.lock
flock -n 8 || { echo 'Another pull is running.'; exit 1; }
if [[ $mode == prepare && -e .env ]]; then
  echo 'Already prepared. Use deploy for updates; keep the existing data and credentials.'; exit 1
fi
if [[ $mode == deploy && ! -f .env ]]; then
  echo 'Prepare and configure credentials before deployment.'; exit 1
fi
if docker info >/dev/null 2>&1; then engine=(docker); else engine=(sudo docker); fi
"${engine[@]}" pull "$image"
# Resolve once and use the immutable image ID for extraction, even if a tag moves.
image_id=$("${engine[@]}" image inspect --format '{{.Id}}' "$image")
revision=$("${engine[@]}" image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image_id")
[[ $revision =~ ^[a-f0-9]{40}$ ]] || { echo 'Missing image revision label.'; exit 1; }
if [[ $image == *:sha-* && ${image##*:sha-} != "$revision" ]]; then
  echo 'Image revision does not match the requested tag.'; exit 1
fi
digest=$("${engine[@]}" image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image_id" |
  awk -v prefix="$repository@sha256:" 'index($0,prefix)==1 { print; exit }')
[[ $digest =~ ^ghcr\.io/tommy-chen-nz/telegram-mail-test@sha256:[a-f0-9]{64}$ ]] || exit 1
release="$root/releases/$revision"
mkdir -p "$release"
container=''
trap 'if [[ -n $container ]]; then "${engine[@]}" rm "$container" >/dev/null; fi' EXIT
# This container is never started. Host networking also applies to create on cand5.
container=$("${engine[@]}" create --network host "$image_id")
"${engine[@]}" cp "$container:/opt/deployment/." - | tar --no-same-owner -xf - -C "$release"
"${engine[@]}" rm "$container" >/dev/null
container=''
if [[ $mode == prepare ]]; then
  cp "$release/compose.yaml" "$root/compose.yaml"
  cp "$release/prepare.sh" "$root/prepare.sh"
  sh "$root/prepare.sh"
  printf 'MAIL_AGENT_IMAGE=%s\nCOMPOSE_PROJECT_NAME=%s\n' "$digest" "$project" >> .env
  echo 'PULL_READY: configure and verify Telegram, Gmail, model, and dashboard before deploy.'
else
  bash "$release/scripts/deploy-server.sh" "$root" "$revision" "$digest" "$webhook" "$project"
fi
# Keep terminal setup tools aligned with the chosen application release.
for file in mailagent.py dashboard.py demo_dashboard.py webhook.py requirements.txt prepare.sh; do
  cp "$release/$file" "$root/$file"
done
cp "$release/scripts/deploy-server.sh" "$root/scripts/deploy-server.sh"
cp "$release/scripts/pull-release.sh" "$root/scripts/pull-release.sh"
for helper in compose.ngrok.yaml ngrok_setup.py gmail_api.py; do
  if [[ -f "$release/$helper" ]]; then cp "$release/$helper" "$root/$helper"; fi
done
