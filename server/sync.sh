#!/usr/bin/env bash
# Run from cron or manually. No computation job is submitted or modified.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCK="/home/ljr/server/.sync.lock"
exec 9>"$LOCK"
flock -n 9 || exit 0

if [ -f "$SCRIPT_DIR/github.env" ]; then
    set -a
    source "$SCRIPT_DIR/github.env"
    set +a
fi
export GITHUB_OWNER="Leejero"
export GITHUB_REPO="hpc-task-status"
export GITHUB_BRANCH="main"
export PYTHONDONTWRITEBYTECODE=1

python3 "$SCRIPT_DIR/collect_status.py" || exit 1
python3 "$SCRIPT_DIR/push_status.py" "$SCRIPT_DIR/data/status.json" || exit 1
