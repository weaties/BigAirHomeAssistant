#!/usr/bin/env bash
# Pull the Pi's encrypted restic repo to this Mac (#111). Run hourly by launchd
# (com.bigair.backup-pull, installed by deploy/mac/setup.sh); safe by hand.
#
# Pulls only when the Pi answers and its last nightly backup checked OK, so a
# failed or half-written night never replaces a good copy. Then, if restic is
# installed and RESTIC_PASSWORD is in deploy/.env.mac, it lists the newest
# snapshot from the Mac's copy, proving it can be read without the Pi.
set -uo pipefail

repo="$(cd "$(dirname "$0")/../.." && pwd)"
envfile="$repo/deploy/.env.mac"
mirror="${BACKUP_MIRROR:-$HOME/Backups/bigair-restic}"
remote="BigAirHomeAssistant/data/backup"
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)
log() { echo "$(date '+%F %T') $*"; }

with_timeout() {  # with_timeout SECONDS cmd...: macOS has no timeout(1)
  local secs="$1"
  shift
  "$@" &
  local pid=$!
  (sleep "$secs" && kill "$pid" 2>/dev/null) &
  local watcher=$!
  wait "$pid"
  local rc=$?
  kill "$watcher" 2>/dev/null
  return $rc
}

# A Tailscale SSH "check" prompt would hang a plain ssh; cap it and move on.
if ! status="$(with_timeout 45 ssh "${ssh_opts[@]}" bigair-ha cat "$remote/status.json" 2>/dev/null)"; then
  log "Pi not reachable (or Tailscale SSH wants a check); will retry next hour"
  exit 0
fi
if ! python3 -c 'import json, sys; sys.exit(0 if json.loads(sys.argv[1]).get("ok") else 1)' "$status"; then
  log "Pi's last backup did not check OK; keeping the current copy. Status: $status"
  exit 1
fi
finished="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["finished"])' "$status")"
if [[ -f "$mirror.status.json" ]] && grep -q "\"finished\": \"$finished\"" "$mirror.status.json"; then
  exit 0  # already have this night's backup
fi

mkdir -p "$mirror"
chmod 700 "$mirror"
log "pulling backup finished $finished"
if ! with_timeout 3600 rsync -a --delete-after -e "ssh ${ssh_opts[*]}" \
  "bigair-ha:$remote/restic/" "$mirror/"; then
  log "rsync failed; will retry next hour"
  exit 1
fi
printf '%s\n' "$status" >"$mirror.status.json"

pw="$(grep -E '^RESTIC_PASSWORD=' "$envfile" 2>/dev/null | cut -d= -f2-)"
restic_bin="$(command -v restic || ls /opt/homebrew/bin/restic 2>/dev/null || true)"
if [[ -n "$pw" && -n "$restic_bin" ]]; then
  if RESTIC_PASSWORD="$pw" "$restic_bin" -r "$mirror" snapshots --latest 1 --compact >/dev/null; then
    log "pulled and readable on the Mac: $(RESTIC_PASSWORD="$pw" "$restic_bin" -r "$mirror" \
      snapshots --latest 1 --compact | sed -n 3p)"
  else
    log "pulled, but restic can't read the Mac copy; check RESTIC_PASSWORD in deploy/.env.mac"
    exit 1
  fi
else
  log "pulled (install restic and set RESTIC_PASSWORD in deploy/.env.mac to verify it)"
fi
