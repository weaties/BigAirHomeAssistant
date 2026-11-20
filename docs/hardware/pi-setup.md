# Raspberry Pi setup (headless)

No keyboard or monitor needed: everything goes over SSH or a web browser.
Covers issue #15. The bootstrap is written to be safe to re-run, but it
hasn't been run on a real Pi yet. Report anything that breaks.

## 1. Flash the SD card (on the Mac)

1. Open **Raspberry Pi Imager**.
   - Device: **Raspberry Pi 5**
   - OS: **Raspberry Pi OS (other) → Raspberry Pi OS Lite (64-bit)**
   - Storage: the SD card
2. **Next → Edit settings** (OS customisation):
   - **General:** hostname `bigair`; your username and a password; Wi-Fi set to
     your **home** network for now; time zone and keyboard layout.
   - **Services:** enable SSH, then **Allow public-key authentication only**,
     and paste your public key (`pbcopy < ~/.ssh/id_ed25519.pub` copies it).
3. Write the card, put it in the Pi, power on, and wait 2–3 minutes.

## 2. First login

```bash
ssh <user>@bigair.local
```

If `bigair.local` doesn't resolve, find the Pi's IP in your router's client
list and use `ssh <user>@<ip>`.

## 3. Run the bootstrap (on the Pi)

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/weaties/BigAirHomeAssistant.git
cd BigAirHomeAssistant
./deploy/bootstrap.sh
```

What it does, in order (each step is skipped if already done):

1. **Preflight:** confirms a 64-bit OS and the Pi model, and warns if booted
   from the SD card.
2. **Packages:** git, jq, chrony and openssl from Debian; Docker Engine and the
   Compose plugin from Docker's official Debian repository. Adds you to the
   `docker` group.
3. **Docker log rotation** (10 MB × 3 per container) to spare the SD card.
4. **Secrets:** creates `deploy/.env` (mode 600) with freshly generated
   passwords and tokens, and `homeassistant/secrets.yaml`. Nothing is printed.
5. **Starts Mosquitto, Home Assistant and InfluxDB.**
6. **Provisions InfluxDB:** the `van_history` bucket, InfluxQL DBRP mappings for
   both buckets, a read-only token for Grafana and a write token for bigair.
7. **Starts Grafana** with the provisioned datasources and dashboards.
8. **Health checks:** both dashboards present, both datasources healthy,
   Mosquitto answering, Home Assistant up.

gpsd and the bigair service are **not** started yet. They're Compose profiles
that wait for the GPS (issue #22, `docs/hardware/gps-xgps160-trial.md`) and
the M1 services.

## 4. After the bootstrap (in a browser)

| What | URL | First step |
|---|---|---|
| Home Assistant | `http://bigair.local:8123` | Create your account, then **Settings → Devices & services → Add integration → MQTT**, broker `localhost`, port `1883`, no username yet (issue #19). HA uses host networking, so it's `localhost`, not `mosquitto`. |
| Grafana | `http://bigair.local:3000` | User `admin`; password from `grep GRAFANA_ADMIN_PASSWORD deploy/.env` on the Pi. Check **BigAir** folder → *Engine history* and *MPG map*. They'll be empty until data flows. |
| InfluxDB | `http://bigair.local:8086` | User `admin`; password from `grep INFLUX_ADMIN_PASSWORD deploy/.env`. Rarely needed. |

Then install the **Home Assistant Companion app** on your iPhone and connect it
to `http://bigair.local:8123` while on the same Wi-Fi.

## Updating

```bash
cd ~/BigAirHomeAssistant && git pull && ./deploy/bootstrap.sh
```

## Adding the van's Wi-Fi later

The van network isn't decided yet, and the bootstrap never touches
networking. When it is, add it alongside home Wi-Fi so the Pi joins whichever
is in range:

```bash
sudo nmcli dev wifi connect "<van-ssid>" password "<password>"
nmcli connection show            # both networks saved; NetworkManager auto-connects
```

If the Pi itself becomes the hotspot, that's a separate, deliberate change.
It's recorded as a CLAUDE.md Decision when made.

## Moving from SD card to SSD (before the van install)

InfluxDB writes constantly and SD cards wear out (CLAUDE.md Decisions #6 and
#25). When the NVMe HAT and SSD arrive:

1. `cd ~/BigAirHomeAssistant && sudo docker compose -f deploy/docker-compose.yml down`
2. Either clone the SD card to the SSD (e.g. `rpi-clone`), **or** re-image
   the SSD with Imager, re-run the bootstrap, and copy across `deploy/.env`,
   `homeassistant/` (including `.storage/`) and `deploy/volumes/`.
3. Set the boot order to NVMe first (`sudo raspi-config` → Advanced → Boot Order).

## Troubleshooting

- **Docker repo fails for your OS codename:** Docker may not publish packages
  for a brand-new Raspberry Pi OS release yet. Check
  `https://download.docker.com/linux/debian/dists/` for the codename in
  `/etc/os-release`.
- **A service won't start:** `cd ~/BigAirHomeAssistant/deploy && sudo docker compose logs <service>`
  (`mosquitto`, `homeassistant`, `influxdb`, `grafana`).
- **Grafana datasource unhealthy:** check that `INFLUX_GRAFANA_TOKEN` is set in
  `deploy/.env`. If it's empty, re-run the bootstrap.

## Letting Claude Code run it

Once `ssh <user>@bigair.local` works from the Mac without a password, Claude
Code can run these steps over SSH from the Mac. It confirms before each step
that changes the Pi.
