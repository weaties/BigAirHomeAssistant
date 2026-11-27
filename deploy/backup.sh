#!/usr/bin/env bash
# Nightly backup of the Pi's data and configuration (#111). Run by
# bigair-backup.timer as root; safe to run by hand: sudo deploy/backup.sh
#
# 1. Stage a consistent copy in $BACKUP_DIR/staging:
#    - InfluxDB: `influx backup` (all buckets + metadata)
#    - SQLite (HA recorder, Grafana): the online backup API, safe while running
#    - HA .storage + secrets.yaml, bigair state (data/bigair/*.json), deploy/.env
# 2. restic backup into an encrypted repo ($BACKUP_DIR/restic), then
#    forget --prune (7 daily, 4 weekly, 6 monthly) and check.
# 3. Write $BACKUP_DIR/status.json. The Mac pulls the repo only when it says ok.
#
# Not backed up: anything in git, the POI notes (the vault is the source),
# byways/public-land GeoJSON (re-downloadable), Docker images, Mosquitto's
# retained messages (display copies only, Decision #22).
set -Eeuo pipefail  # -E: the ERR trap also fires inside functions

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO/deploy/.env"
BACKUP_DIR="${BACKUP_DIR:-$REPO/data/backup}"
STAGE="$BACKUP_DIR/staging"
STATUS="$BACKUP_DIR/status.json"
OWNER="$(stat -c %U "$REPO")"  # the Mac pulls as this user over SSH
COMPOSE=(docker compose -f "$REPO/deploy/docker-compose.yml")
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

env_get() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//'; }

status() {  # status ok|failed message [snapshot]
  python3 - "$STATUS" "$1" "$2" "${3:-}" "$started" <<'EOF'
import json, os, sys, datetime
path, result, message, snapshot, started = sys.argv[1:]
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump({
        "ok": result == "ok",
        "message": message,
        "snapshot": snapshot or None,
        "started": started,
        "finished": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, f)
os.replace(tmp, path)
EOF
  chown "$OWNER" "$STATUS"
}
fail() { echo "backup FAILED: $*" >&2; status failed "$*"; exit 1; }
trap 'fail "line $LINENO: $BASH_COMMAND"' ERR

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
command -v restic >/dev/null || fail "restic not installed (re-run deploy/bootstrap.sh)"
RESTIC_PASSWORD="$(env_get RESTIC_PASSWORD)"
[[ -n "$RESTIC_PASSWORD" ]] || fail "RESTIC_PASSWORD missing from deploy/.env"
export RESTIC_PASSWORD RESTIC_REPOSITORY="$BACKUP_DIR/restic"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
rm -rf "$STAGE"
mkdir -p "$STAGE"/{influx,homeassistant,grafana,bigair,deploy}

# --- InfluxDB -----------------------------------------------------------------
token="$(env_get INFLUX_ADMIN_TOKEN)"
"${COMPOSE[@]}" exec -T influxdb rm -rf /tmp/bigair-backup
"${COMPOSE[@]}" exec -T influxdb influx backup /tmp/bigair-backup \
  --host http://localhost:8086 --token "$token" >/dev/null
"${COMPOSE[@]}" cp influxdb:/tmp/bigair-backup/. "$STAGE/influx/"
"${COMPOSE[@]}" exec -T influxdb rm -rf /tmp/bigair-backup

# --- SQLite, consistent while the apps run -------------------------------------
sqlite_copy() {  # src dest
  [[ -f "$1" ]] || return 0
  python3 -c 'import sqlite3, sys
s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2])
with d: s.backup(d)
d.close(); s.close()' "$1" "$2"
}
sqlite_copy "$REPO/homeassistant/home-assistant_v2.db" "$STAGE/homeassistant/home-assistant_v2.db"
sqlite_copy "$REPO/deploy/volumes/grafana/grafana.db" "$STAGE/grafana/grafana.db"

# --- Files ---------------------------------------------------------------------
cp -a "$REPO/homeassistant/.storage" "$STAGE/homeassistant/" 2>/dev/null || true
cp -a "$REPO/homeassistant/secrets.yaml" "$REPO/homeassistant/ip_bans.yaml" \
  "$STAGE/homeassistant/" 2>/dev/null || true
find "$REPO/data/bigair" -maxdepth 1 -type f \( -name '*.json' -o -name '*.yaml' \) \
  -exec cp -a {} "$STAGE/bigair/" \;
cp -a "$ENV_FILE" "$STAGE/deploy/.env"
git -C "$REPO" rev-parse HEAD >"$STAGE/deploy/git-head" 2>/dev/null || true

# --- restic --------------------------------------------------------------------
if [[ ! -f "$RESTIC_REPOSITORY/config" ]]; then
  restic init >/dev/null
  echo "initialised restic repo at $RESTIC_REPOSITORY"
fi
snap="$(restic backup --host bigair-ha --tag nightly --json --quiet "$STAGE" |
  python3 -c 'import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if m.get("message_type") == "summary":
        print(m["snapshot_id"][:8])')"
restic forget --host bigair-ha --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune --quiet
restic check --quiet
rm -rf "$STAGE"
chown -R "$OWNER" "$RESTIC_REPOSITORY"
status ok "snapshot $snap" "$snap"
echo "backup ok: snapshot $snap"
