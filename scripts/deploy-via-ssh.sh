#!/usr/bin/env bash
set -euo pipefail
umask 077

# All values enter through environment variables, never workflow expressions in shell code.
[[ ${DEPLOY_HOST:-} =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || { echo 'Invalid DEPLOY_HOST'; exit 1; }
[[ ${DEPLOY_USER:-} =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]] || { echo 'Invalid DEPLOY_USER'; exit 1; }
[[ ${DEPLOY_PORT:-} =~ ^[0-9]{1,5}$ ]] && ((10#$DEPLOY_PORT > 0 && 10#$DEPLOY_PORT < 65536)) || { echo 'Invalid DEPLOY_PORT'; exit 1; }
[[ ${DEPLOY_DIR:-} =~ ^/[a-zA-Z0-9_./-]+$ && "$DEPLOY_DIR" != *'..'* && "$DEPLOY_DIR" != '/' ]] || { echo 'Invalid DEPLOY_DIR'; exit 1; }
[[ ${DEPLOY_PROJECT:-} =~ ^[a-z0-9][a-z0-9_-]*$ ]] || { echo 'Invalid DEPLOY_PROJECT'; exit 1; }
[[ ${GITHUB_SHA:-} =~ ^[a-f0-9]{40}$ ]] || { echo 'Invalid revision'; exit 1; }
[[ ${IMAGE:-} =~ ^ghcr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$ ]] || { echo 'A digest-pinned image is required'; exit 1; }
[[ ${ENABLE_WEBHOOK:-} == true || ${ENABLE_WEBHOOK:-} == false ]] || exit 1
[[ -n ${SSH_PRIVATE_KEY:-} && -n ${SSH_KNOWN_HOSTS:-} ]] || { echo 'Missing SSH secrets'; exit 1; }

ssh_dir=$(mktemp -d)
trap 'rm -f -- "$ssh_dir/key" "$ssh_dir/known_hosts"; rmdir -- "$ssh_dir"' EXIT
printf '%s\n' "$SSH_PRIVATE_KEY" > "$ssh_dir/key"
printf '%s\n' "$SSH_KNOWN_HOSTS" > "$ssh_dir/known_hosts"
unset SSH_PRIVATE_KEY SSH_KNOWN_HOSTS
options=(-i "$ssh_dir/key" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes
         -o "UserKnownHostsFile=$ssh_dir/known_hosts" -o ConnectTimeout=15 -o ServerAliveInterval=15)
destination="$DEPLOY_USER@$DEPLOY_HOST"
release="$DEPLOY_DIR/releases/$GITHUB_SHA"
ssh -p "$DEPLOY_PORT" "${options[@]}" "$destination" "mkdir -p '$release'"
scp -P "$DEPLOY_PORT" "${options[@]}" compose.yaml scripts/deploy-server.sh "$destination:$release/"
ssh -p "$DEPLOY_PORT" "${options[@]}" "$destination" \
  "bash '$release/deploy-server.sh' '$DEPLOY_DIR' '$GITHUB_SHA' '$IMAGE' '$ENABLE_WEBHOOK' '$DEPLOY_PROJECT'"
