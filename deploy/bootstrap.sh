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
# secrets → start broker/HA/InfluxDB → provision InfluxDB (buckets, DBRP,
# tokens) → start Grafana → health checks.
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

health() {
  log "Health checks"
  local ok=1 pass
  pass="$(env_get GRAFANA_ADMIN_PASSWORD)"
  local dashboards
  dashboards="$({ curl -sf -u "admin:$pass" "http://localhost:3000/api/search?query=" || echo '[]'; } \
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

  # shellcheck disable=SC2016  # $SYS is a literal MQTT topic, not a variable
  if "${COMPOSE[@]}" exec -T mosquitto mosquitto_sub -t '$SYS/broker/version' -C 1 -W 5 >/dev/null; then
    echo "Mosquitto: OK"
  else
    warn "Mosquitto did not answer"; ok=0
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
start_core
provision_influx
start_grafana
health
