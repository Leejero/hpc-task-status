#!/usr/bin/env bash
# Install only this collector's marked block; preserve unrelated user cron entries.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp /home/ljr/server/.cron.XXXXXX)"
trap 'rm -f "$TMP"' EXIT
(crontab -l 2>/dev/null || true) | awk '/# BEGIN HPC TASK STATUS/{skip=1;next} /# END HPC TASK STATUS/{skip=0;next} !skip' > "$TMP"
{
    echo "# BEGIN HPC TASK STATUS"
    echo "CRON_TZ=Asia/Shanghai"
    echo "0 8,10,12,14,16,18,20 * * * /usr/bin/flock -n /home/ljr/server/.cron.lock /bin/bash $SCRIPT_DIR/sync.sh >> /home/ljr/server/data/sync.log 2>&1"
    echo "# END HPC TASK STATUS"
} >> "$TMP"
crontab "$TMP"
crontab -l | awk '/# BEGIN HPC TASK STATUS/{show=1} show{print} /# END HPC TASK STATUS/{show=0}'
