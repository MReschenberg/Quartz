#!/bin/bash
#
# Auto-commit and push the Obsidian vault.
# Run nightly by the launchd agent ~/Library/LaunchAgents/com.morgan.git-push.plist
# (launchd runs this on the next wake if the Mac was asleep at the scheduled time).

# launchd gives processes a minimal environment, so set PATH/HOME explicitly.
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
export HOME="/Users/morganraereschenberg"

VAULT="/Users/morganraereschenberg/Library/Mobile Documents/iCloud~md~obsidian/Documents"
LOG="$HOME/git_push.log"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S')  $*" >> "$LOG"; }

cd "$VAULT" || { log "ERROR: cannot cd into vault (Full Disk Access for the launchd process?)"; exit 1; }

date=$(date +"%Y-%m-%d %T")
git add -A

# Commit only if there is something staged (avoids a noisy non-zero exit on a clean tree).
if ! git diff --cached --quiet; then
  git commit -m "Commit for $date" >> "$LOG" 2>&1
  log "committed changes"
else
  log "nothing to commit"
fi

# Push whenever the local branch is ahead of its upstream (branch-name agnostic).
status="$(git status --branch --porcelain | head -1)"
log "status: $status"
if echo "$status" | grep -q "ahead"; then
  log "pushing..."
  git push >> "$LOG" 2>&1 && log "push OK" || log "push FAILED (rc=$?)"
else
  log "nothing to push"
fi
