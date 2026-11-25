#!/usr/bin/env bash
# Mac copy of the trip planner, plus Tailscale relays to the Pi's HA and Grafana (#105).
# The relays are deploy/mac/relay.py under uv's Python, which the macOS firewall
# already allows (a new binary like socat is silently blocked).
#
#   deploy/mac/setup.sh [--copy-keys] [--copy-data]   # install / refresh
#   deploy/mac/setup.sh --remove                       # unload and delete the agents
#
# Reads deploy/.env.mac (see env.mac.example). Installs three launchd agents:
#   com.bigair.planner      bigair planner on MAC_TS_IP:8092 (tank from the Pi's MQTT)
#   com.bigair.relay-ha     MAC_TS_IP:8123 -> PI_TS_IP:8123 (Home Assistant)
#   com.bigair.relay-grafana MAC_TS_IP:3000 -> PI_TS_IP:3000
# Idempotent: re-running rewrites and reloads them.
set -euo pipefail

repo="$(cd "$(dirname "$0")/../.." && pwd)"
envfile="$repo/deploy/.env.mac"
agents="$HOME/Library/LaunchAgents"
logs="$HOME/Library/Logs/bigair"
labels=(com.bigair.planner com.bigair.relay-ha com.bigair.relay-grafana)

unload() {
  for l in "${labels[@]}"; do
    launchctl bootout "gui/$(id -u)/$l" 2>/dev/null || true
  done
}

if [[ "${1:-}" == "--remove" ]]; then
  unload
  for l in "${labels[@]}"; do rm -f "$agents/$l.plist"; done
  echo "removed"
  exit 0
fi

[[ -f "$envfile" ]] || { echo "missing $envfile (copy deploy/mac/env.mac.example)" >&2; exit 1; }

setkey() {  # setkey NAME VALUE: replace NAME=... in the env file
  local tmp
  tmp="$(mktemp)"
  grep -v "^$1=" "$envfile" >"$tmp" || true
  printf '%s=%s\n' "$1" "$2" >>"$tmp"
  mv "$tmp" "$envfile"
  chmod 600 "$envfile"
}

for arg in "$@"; do
  case "$arg" in
    --copy-keys)
      for k in GOOGLE_MAPS_API_KEY EIA_API_KEY ANTHROPIC_API_KEY; do
        # shellcheck disable=SC2029  # $k is meant to expand on this side
        v="$(ssh bigair-ha "grep '^$k=' ~/BigAirHomeAssistant/deploy/.env | cut -d= -f2-" || true)"
        [[ -n "$v" ]] && setkey "$k" "$v"
      done
      ;;
    --copy-data) ;;  # after the env file is loaded
    *) echo "unknown option $arg" >&2; exit 1 ;;
  esac
done

set -a
# shellcheck disable=SC1090
source "$envfile"
set +a
: "${PI_TS_IP:?set PI_TS_IP in $envfile}" "${MAC_TS_IP:?set MAC_TS_IP in $envfile}"
data="${PLANNER_DATA_DIR:-$repo/data/mac}"
mkdir -p "$data" "$logs" "$agents"

if [[ " $* " == *" --copy-data "* ]]; then
  rsync -a bigair-ha:BigAirHomeAssistant/data/bigair/{byways.geojson,public-land.geojson,ioverlander} "$data/"
fi

uv="$(command -v uv)"

xml() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' <<<"$1"; }

plist() {  # plist LABEL ENV_XML ARG...
  local label="$1" envxml="$2"
  shift 2
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0"><dict>'
    echo "<key>Label</key><string>$label</string>"
    echo '<key>ProgramArguments</key><array>'
    for a in "$@"; do echo "<string>$(xml "$a")</string>"; done
    echo '</array>'
    echo "<key>WorkingDirectory</key><string>$(xml "$repo")</string>"
    echo "<key>EnvironmentVariables</key><dict>$envxml</dict>"
    echo '<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>'
    echo '<key>ThrottleInterval</key><integer>15</integer>'
    echo "<key>StandardOutPath</key><string>$logs/$label.log</string>"
    echo "<key>StandardErrorPath</key><string>$logs/$label.log</string>"
    echo '</dict></plist>'
  } >"$agents/$label.plist"
  chmod 600 "$agents/$label.plist"  # holds API keys
}

envxml=""
add() { [[ -n "${2:-}" ]] && envxml+="<key>$1</key><string>$(xml "$2")</string>"; return 0; }
add PATH "/usr/bin:/bin:/usr/sbin:/sbin:$(dirname "$uv")"
add GOOGLE_MAPS_API_KEY "${GOOGLE_MAPS_API_KEY:-}"
add EIA_API_KEY "${EIA_API_KEY:-}"
add ANTHROPIC_API_KEY "${ANTHROPIC_API_KEY:-}"
add CLAUDE_MONTHLY_CAP_USD "${CLAUDE_MONTHLY_CAP_USD:-5}"
add PLANNER_DAILY_LIMIT "${PLANNER_DAILY_LIMIT:-20}"
add POI_DIR "${POI_DIR:-}"
add TZ "${TZ:-}"
add BRIEFING_TIME " "  # the Pi sends the evening briefing; two would double-notify
add PLANNER_URL "http://$MAC_TS_IP:8092/"
[[ -f "$data/byways.geojson" ]] && add BYWAYS_FILE "$data/byways.geojson"
[[ -f "$data/public-land.geojson" ]] && add PUBLIC_LAND_FILE "$data/public-land.geojson"
[[ -d "$data/ioverlander" ]] && add IOVERLANDER_DIR "$data/ioverlander"

unload
plist com.bigair.planner "$envxml" "$uv" run --project "$repo" bigair planner \
  --host "$MAC_TS_IP" --port 8092 --cache "$data/trip-plan.json" --mqtt-host "$PI_TS_IP"
relay() {  # relay LABEL PORT
  plist "$1" "" "$uv" run --project "$repo" python "$repo/deploy/mac/relay.py" \
    "$MAC_TS_IP" "$2" "$PI_TS_IP"
}
relay com.bigair.relay-ha 8123
relay com.bigair.relay-grafana 3000
for l in "${labels[@]}"; do
  launchctl bootstrap "gui/$(id -u)" "$agents/$l.plist"
done

echo "planner:  http://$MAC_TS_IP:8092/"
echo "HA:       http://$MAC_TS_IP:8123/"
echo "Grafana:  http://$MAC_TS_IP:3000/"
echo "logs:     $logs"
