#!/bin/sh
set -eu
cd "$(dirname "$0")"
umask 077
mkdir -p data secrets backups
chmod 700 data secrets backups
python3 - "$(id -u)" "$(id -g)" <<'PY'
from pathlib import Path
import os, sys
path = Path('.env')
lines = path.read_text().splitlines() if path.exists() else []
lines = [line for line in lines if not line.startswith(('LOCAL_UID=', 'LOCAL_GID='))]
temporary = Path('.env.prepare.tmp')
temporary.write_text('\n'.join(lines + ['LOCAL_UID=' + sys.argv[1], 'LOCAL_GID=' + sys.argv[2]]) + '\n')
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PY
python3 --version
docker compose version
printf '\nREADY: python3 mailagent.py setup-telegram\n'
