#!/usr/bin/env bash
# BigAir Pi bootstrap — run ON the Raspberry Pi, from inside the cloned repo:
#
#   git clone https://github.com/weaties/BigAirHomeAssistant.git
#   cd BigAirHomeAssistant && ./deploy/bootstrap.sh
#
# Safe to re-run: every step checks before it changes anything. It never
# touches networking (Wi-Fi stays as Raspberry Pi Imager set it).
# Secrets are generated on the Pi into deploy/.env (gitignored, mode 600) and
# are never printed.
#
# Steps: preflight → packages (Docker, git, jq, chrony) → Docker log limits →
# secrets → GPS (host gpsd + Bluetooth binding, if GPS_BT_MAC is set) →
# start broker/HA/InfluxDB → provision InfluxDB (buckets, DBRP, tokens) →
# start Grafana → health checks.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY="$REPO/deploy"
ENV_FILE="$DEPLOY/.env"
COMPOSE=(sudo docker compose --project-directory "$DEPLOY" -f "$DEPLOY/docker-compose.yml")
ORG="bigair"

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARN: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

env_get() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//'; }
env_set() {  # replace KEY=... in place (value must not contain '|')
  local key="$1" value="$2"
  if grep -qE "^$key=" "$ENV_FILE"; then
    sed -i "s|^$key=.*|$key=$value|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}
rand_hex()  { openssl rand -hex "$1"; }
rand_pass() { openssl rand -base64 32 | tr -d '/+=' | cut -c1-24; }
install_if_changed() {  # src dest → returns 0 if it installed (content differed)
  if sudo cmp -s "$1" "$2"; then return 1; fi
  sudo install -D -m 0644 "$1" "$2"
}

# --------------------------------------------------------------------------
preflight() {
  log "Preflight"
  [[ "$(uname -m)" == "aarch64" ]] || die "expected a 64-bit OS (aarch64), got $(uname -m)"
  [[ $EUID -ne 0 ]] || die "run as your normal user (the script uses sudo where needed)"
  local model
  model="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
  echo "Model: $model"
  [[ "$model" == *"Raspberry Pi 5"* ]] || warn "not a Raspberry Pi 5 — continuing anyway"
  # shellcheck disable=SC1091
  . /etc/os-release
  echo "OS: $PRETTY_NAME"
  local root_dev
  root_dev="$(findmnt -n -o SOURCE /)"
  if [[ "$root_dev" == /dev/mmcblk* ]]; then
    warn "booting from SD card ($root_dev) — fine for bring-up; move to SSD before the van install (CLAUDE.md Decision #25)"
  fi
}

install_packages() {
  log "Packages: git, jq, chrony, openssl, curl"
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git jq chrony openssl curl ca-certificates

  if command -v docker >/dev/null && sudo docker compose version >/dev/null 2>&1; then
    echo "Docker already installed: $(sudo docker --version)"
  else
    log "Docker Engine + Compose plugin (Docker's official Debian repository)"
    local codename
    # shellcheck disable=SC1091  # /etc/os-release exists only on the Pi
    codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $codename stable" \
      | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
    sudo apt-get update -qq || die "Docker's repo may not support '$codename' yet — see docs/hardware/pi-setup.md"
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
      docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi
  if ! id -nG "$USER" | grep -qw docker; then
    sudo usermod -aG docker "$USER"
    echo "Added $USER to the docker group (takes effect at next login; this script uses sudo)."
  fi
}

docker_log_limits() {
  log "Docker log rotation (protects the SD card)"
  local conf=/etc/docker/daemon.json
  if [[ -f $conf ]] && grep -q '"max-size"' "$conf"; then
    echo "Already configured."
    return
  fi
  [[ -f $conf ]] && die "$conf exists without log limits — merge by hand: {\"log-driver\":\"json-file\",\"log-opts\":{\"max-size\":\"10m\",\"max-file\":\"3\"}}"
  echo '{"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"}}' | sudo tee "$conf" >/dev/null
  sudo systemctl restart docker
}

secrets() {
  log "Secrets (deploy/.env, homeassistant/secrets.yaml)"
  if [[ ! -f "$ENV_FILE" ]]; then
    cp "$DEPLOY/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    env_set TZ "$(timedatectl show -p Timezone --value)"
    env_set INFLUX_ADMIN_PASSWORD "$(rand_pass)"
    env_set INFLUX_ADMIN_TOKEN "$(rand_hex 32)"
    env_set GRAFANA_ADMIN_PASSWORD "$(rand_pass)"
    echo "Created deploy/.env with fresh secrets (not printed)."
  else
    echo "deploy/.env exists — keeping it."
  fi
  local ha="$REPO/homeassistant/secrets.yaml"
  if [[ ! -f "$ha" ]]; then
    sed -e "s|^time_zone:.*|time_zone: $(timedatectl show -p Timezone --value)|" \
        -e "s|^internal_url:.*|internal_url: http://$(hostname).local:8123|" \
        "$REPO/homeassistant/secrets.yaml.example" > "$ha"
    chmod 600 "$ha"
    echo "Created homeassistant/secrets.yaml."
  else
    echo "homeassistant/secrets.yaml exists — keeping it."
  fi
  mkdir -p "$DEPLOY/volumes/grafana"
  sudo chown 472:0 "$DEPLOY/volumes/grafana"  # Grafana's container user
}

gps_host() {
  log "GPS: host gpsd + Bluetooth RFCOMM binding (Decision #28)"
  local mac ch changed=0 unit
  mac="$(env_get GPS_BT_MAC | tr '[:lower:]' '[:upper:]')"
  ch="$(env_get GPS_BT_CHANNEL)"; ch="${ch:-1}"
  if [[ -z "$mac" ]]; then
    echo "GPS_BT_MAC not set in deploy/.env — skipping (see docs/hardware/gps-xgps160-trial.md)."
    return
  fi
  [[ "$mac" =~ ^([0-9A-F]{2}:){5}[0-9A-F]{2}$ ]] || die "GPS_BT_MAC is not a MAC address"
  [[ "$ch" =~ ^[0-9]+$ ]] || die "GPS_BT_CHANNEL must be a number"
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gpsd gpsd-clients >/dev/null
  bluetoothctl trust "$mac" >/dev/null 2>&1 \
    || warn "could not trust the GPS — pair it first (bluetoothctl pair), receiver powered on"

  unit="$(mktemp)"
  sed -e "s/@GPS_BT_MAC@/$mac/" -e "s/@GPS_BT_CHANNEL@/$ch/" "$DEPLOY/host/rfcomm-gps.service" > "$unit"
  install_if_changed "$unit" /etc/systemd/system/rfcomm-gps.service && changed=1
  rm -f "$unit"
  install_if_changed "$DEPLOY/host/gpsd.default" /etc/default/gpsd && changed=1
  install_if_changed "$DEPLOY/host/gpsd-bigair.conf" /etc/systemd/system/gpsd.service.d/bigair.conf && changed=1

  sudo systemctl daemon-reload
  sudo systemctl enable rfcomm-gps.service gpsd.socket gpsd.service >/dev/null 2>&1
  if [[ $changed == 1 ]] || ! systemctl is-active -q gpsd.service; then
    sudo systemctl restart rfcomm-gps.service gpsd.service
    echo "GPS config installed/changed — services restarted."
  else
    echo "GPS config unchanged."
  fi
  echo "rfcomm-gps: $(systemctl is-active rfcomm-gps.service)  gpsd: $(systemctl is-active gpsd.service)"

  # System time from GPS (Decision #24, #22): chrony reads gpsd's SHM 0 segment.
  if install_if_changed "$DEPLOY/host/chrony-gps.conf" /etc/chrony/conf.d/bigair-gps.conf; then
    sudo systemctl restart chrony
    echo "chrony GPS refclock installed — chrony restarted."
  else
    echo "chrony GPS refclock unchanged."
  fi
}

tailscale_host() {
  log "Tailscale (Decision #30)"
  if [[ "$(env_get TAILSCALE_ENABLE)" != "true" ]]; then
    echo "TAILSCALE_ENABLE is not true in deploy/.env — skipping."
    return
  fi
  if ! command -v tailscale >/dev/null; then
    local codename
    # shellcheck disable=SC1091  # /etc/os-release exists only on the Pi
    codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
    sudo curl -fsSL "https://pkgs.tailscale.com/stable/debian/$codename.noarmor.gpg" \
      -o /usr/share/keyrings/tailscale-archive-keyring.gpg
    sudo curl -fsSL "https://pkgs.tailscale.com/stable/debian/$codename.tailscale-keyring.list" \
      -o /etc/apt/sources.list.d/tailscale.list
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tailscale >/dev/null
  fi
  sudo systemctl enable --now tailscaled >/dev/null 2>&1
  local state
  state="$(tailscale status --json 2>/dev/null | jq -r '.BackendState // "Unknown"' || echo Unknown)"
  if [[ "$state" == "Running" ]]; then
    echo "Tailscale: up as $(tailscale status --json | jq -r '.Self.DNSName' | sed 's/\.$//')"
    tailscale_preview
    return
  fi
  local authkey
  authkey="$(env_get TAILSCALE_AUTHKEY)"
  # --accept-dns=false: the van must work offline and Docker's DNS must not depend
  # on tailscaled (MagicDNS would rewrite /etc/resolv.conf). Tailscale is remote
  # access only; nothing in the stack depends on it.
  if [[ -n "$authkey" ]]; then
    sudo tailscale up --ssh --accept-dns=false --hostname "$(hostname)" --authkey "$authkey"
    echo "Tailscale: joined with the auth key from deploy/.env"
    tailscale_preview
  else
    echo "Tailscale installed but not logged in (state: $state). Join your tailnet with:"
    echo "  sudo tailscale up --ssh --accept-dns=false --hostname $(hostname)"
    echo "and open the login URL it prints (or set TAILSCALE_AUTHKEY in deploy/.env)."
  fi
}

# Dash preview (#62, Decision #37). The preview server has no key, so Compose
# publishes it on 127.0.0.1 only and `tailscale serve` exposes it to the tailnet,
# never the van LAN. `serve --bg` persists across reboots; re-running is a no-op.
tailscale_preview() {
  local port
  port="$(env_get DISPLAY_PREVIEW_PORT)"
  port="${port:-8091}"
  sudo tailscale serve --bg --http="$port" "http://127.0.0.1:$port" >/dev/null
  echo "Dash preview (tailnet only): http://$(tailscale ip -4 | head -1):$port/"
}

start_core() {
  log "Starting Mosquitto, Home Assistant, InfluxDB"
  "${COMPOSE[@]}" up -d mosquitto homeassistant influxdb
  echo -n "Waiting for InfluxDB"
  for _ in $(seq 1 60); do
    if curl -sf http://localhost:8086/health | grep -q '"status":"pass"'; then echo " ok"; return; fi
    echo -n "."; sleep 2
  done
  die "InfluxDB did not become healthy — check: ${COMPOSE[*]} logs influxdb"
}

influx() {  # run the influx CLI inside the container as admin
  "${COMPOSE[@]}" exec -T influxdb influx "$@" --host http://localhost:8086 --token "$(env_get INFLUX_ADMIN_TOKEN)"
}

bucket_id() { { influx bucket list --org "$ORG" --name "$1" --json 2>/dev/null || true; } | jq -r '.[0].id? // empty'; }
token_of() { jq -r 'if type == "array" then .[0].token else .token end'; }

provision_influx() {
  log "Provisioning InfluxDB (buckets, InfluxQL DBRP mappings, tokens)"
  [[ -n "$(bucket_id van_history)" ]] || influx bucket create --org "$ORG" --name van_history --retention 0 >/dev/null
  local raw hist
  raw="$(bucket_id van_raw)"; hist="$(bucket_id van_history)"
  [[ -n "$raw" && -n "$hist" ]] || die "buckets missing after provisioning"
  echo "van_raw=$raw van_history=$hist"

  local db id
  for db in van_raw van_history; do
    [[ $db == van_raw ]] && id="$raw" || id="$hist"
    local count
    count="$({ influx v1 dbrp list --org "$ORG" --db "$db" --json 2>/dev/null || true; } \
      | jq 'if . == null then 0 else length end' 2>/dev/null || true)"
    if [[ -z "$count" || "$count" == "0" ]]; then
      influx v1 dbrp create --org "$ORG" --db "$db" --rp autogen --bucket-id "$id" --default >/dev/null
      echo "DBRP mapping created for $db"
    fi
  done

  if [[ -z "$(env_get INFLUX_GRAFANA_TOKEN)" ]]; then
    env_set INFLUX_GRAFANA_TOKEN "$(influx auth create --org "$ORG" --description grafana-read \
      --read-bucket "$raw" --read-bucket "$hist" --json | token_of)"
    echo "Created read-only Grafana token."
  fi
  if [[ -z "$(env_get INFLUX_BIGAIR_TOKEN)" ]]; then
    env_set INFLUX_BIGAIR_TOKEN "$(influx auth create --org "$ORG" --description bigair-write \
      --read-bucket "$raw" --read-bucket "$hist" --write-bucket "$raw" --write-bucket "$hist" --json | token_of)"
    echo "Created bigair write token."
  fi
}

start_grafana() {
  log "Starting Grafana"
  "${COMPOSE[@]}" up -d --force-recreate grafana
  echo -n "Waiting for Grafana"
  for _ in $(seq 1 60); do
    if curl -sf http://localhost:3000/api/health >/dev/null; then echo " ok"; return; fi
    echo -n "."; sleep 2
  done
  die "Grafana did not start — check: ${COMPOSE[*]} logs grafana"
}

start_display() {
  log "Dash display server (Decision #29)"
  if [[ -z "$(env_get DISPLAY_API_KEY)" ]]; then
    echo "DISPLAY_API_KEY not set in deploy/.env — skipping (see deploy/.env.example)."
    return
  fi
  "${COMPOSE[@]}" up -d --build display
  echo -n "Waiting for the display server"
  for _ in $(seq 1 60); do
    if curl -sf http://localhost:8090/health >/dev/null; then echo " ok"; return; fi
    echo -n "."; sleep 2
  done
  die "display server did not start — check: ${COMPOSE[*]} logs display"
}

start_bigair() {
  log "Fuel service (#53)"
  mkdir -p "$REPO/data/bigair"
  "${COMPOSE[@]}" up -d --build bigair
  sleep 5
  if [[ "$(docker inspect -f '{{.State.Running}}' bigair-bigair-1 2>/dev/null)" != "true" ]]; then
    die "fuel service is not running — check: ${COMPOSE[*]} logs bigair"
  fi
  echo "running (tank starts unknown until 'Filled to full' is pressed in Home Assistant)"
}

health() {
  log "Health checks"
  local ok=1 pass
  pass="$(env_get GRAFANA_ADMIN_PASSWORD)"
  local dashboards
  dashboards="$({ curl -sf -u "admin:$pass" "http://localhost:3000/api/search?type=dash-db" || echo '[]'; } \
    | jq -r '.[].uid' | sort | tr '\n' ' ')"
  echo "Grafana dashboards: ${dashboards:-none}"
  [[ "$dashboards" == *bigair-engine-history* && "$dashboards" == *bigair-mpg-map* ]] || { warn "provisioned dashboards missing"; ok=0; }

  local ds
  for ds in influx-van-raw influx-van-history; do
    if curl -sf -u "admin:$pass" "http://localhost:3000/api/datasources/uid/$ds/health" | jq -e '.status == "OK"' >/dev/null; then
      echo "Datasource $ds: OK"
    else
      warn "datasource $ds failed its health check"; ok=0
    fi
  done

  if [[ -n "$(env_get DISPLAY_API_KEY)" ]]; then
    local bytes  # the firmware only accepts a full 259200-byte frame
    bytes="$(curl -sf -H "Authorization: Bearer $(env_get DISPLAY_API_KEY)" \
      http://localhost:8090/api/display.raw | wc -c | tr -d ' ' || true)"
    if [[ "$bytes" == "259200" ]]; then
      echo "Dash display frame: OK (259200 bytes)"
    else
      warn "dash display frame wrong size (${bytes:-none} bytes)"; ok=0
    fi
  fi

  # shellcheck disable=SC2016  # $SYS is a literal MQTT topic, not a variable
  if "${COMPOSE[@]}" exec -T mosquitto mosquitto_sub -t '$SYS/broker/version' -C 1 -W 5 >/dev/null; then
    echo "Mosquitto: OK"
  else
    warn "Mosquitto did not answer"; ok=0
  fi

  if [[ -n "$(env_get GPS_BT_MAC)" ]]; then
    local mode  # report fix mode only, never coordinates
    mode="$(timeout 20 gpspipe -w -n 40 127.0.0.1:2947 2>/dev/null \
      | jq -r 'select(.class == "TPV") | .mode' 2>/dev/null | tail -1 || true)"
    case "$mode" in
      3) echo "GPS: 3D fix" ;;
      2) echo "GPS: 2D fix (weak sky view?)" ;;
      *) warn "GPS: no fix from gpsd (mode='${mode:-none}'). Receiver on? See docs/hardware/gps-xgps160-trial.md"; ok=0 ;;
    esac
    local reach ref
    # `chronyc sources` columns: MS Name Stratum Poll Reach … — reach is $5 (octal
    # shift register of the last 8 polls), not $4 (poll interval).
    reach="$(chronyc -n sources 2>/dev/null | awk '$2 == "GPS" {print $5}')"
    ref="$(chronyc -n tracking 2>/dev/null | sed -n 's/^Reference ID *: //p')"
    if [[ -n "$reach" && "$reach" != "0" ]]; then
      echo "chrony: GPS refclock receiving (reach $reach); clock reference: $ref"
    else
      warn "chrony: GPS refclock not receiving yet (reach '${reach:-missing}'); check 'chronyc sources' in a minute"
    fi
  fi

  echo -n "Home Assistant (first start can take a few minutes)"
  for _ in $(seq 1 90); do
    if curl -s -o /dev/null -w '%{http_code}' http://localhost:8123 | grep -qE '200|302'; then echo " ok"; break; fi
    echo -n "."; sleep 2
  done

  local host; host="$(hostname).local"
  cat <<EOF

Done$([[ $ok == 1 ]] || echo " (with warnings above)").
  Home Assistant: http://$host:8123   (create your account in the browser)
  Grafana:        http://$host:3000   (user admin; password: grep GRAFANA_ADMIN_PASSWORD deploy/.env)
  InfluxDB:       http://$host:8086   (user admin; password: grep INFLUX_ADMIN_PASSWORD deploy/.env)
Next: docs/hardware/pi-setup.md → "After the bootstrap".
EOF
}

preflight
install_packages
docker_log_limits
secrets
gps_host
tailscale_host
start_core
provision_influx
start_grafana
start_display
start_bigair
health
