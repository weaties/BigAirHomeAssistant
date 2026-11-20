# Raspberry Pi setup (headless)

No keyboard or monitor needed: everything goes over SSH or a web browser.
Covers issue #15.

**Verified 2026-11-19** on a Pi 5 Model B Rev 1.1 running Raspberry Pi OS Lite
64-bit (Debian 13 "trixie"), booting from an SD card. The first run passed
every health check, and a second run changed nothing.

**Power:** use a 5 V / 5 A USB-C supply (the official 27 W one). On a 3 A
supply the Pi runs, but it limits USB to 600 mA and has less headroom. Our
first boot on a 3 A supply dropped to standby (solid red LED) once.
`vcgencmd get_throttled` should read `0x0`.

## 1. Flash the SD card (on the Mac)

1. Open **Raspberry Pi Imager**.
   - Device: **Raspberry Pi 5**
   - OS: **Raspberry Pi OS (other) → Raspberry Pi OS Lite (64-bit)**
   - Storage: the SD card
2. **Next → Edit settings** (OS customisation):
   - **General:** hostname `bigair-ha`; your username and a password; Wi-Fi set to
     your **home** network for now; time zone and keyboard layout.
   - **Services:** enable SSH, then **Allow public-key authentication only**,
     and paste your public key (`pbcopy < ~/.ssh/id_ed25519.pub` copies it).
     **Check which key is in that box.** Imager pre-fills one it finds
     (here it chose `~/.ssh/id_rsa.pub`), and SSH fails with
     `Permission denied (publickey)` if you then use a different key.
3. Write the card, put it in the Pi, power on, and wait 2–3 minutes.

## 2. First login

```bash
ssh <user>@bigair-ha.local
```

If `bigair-ha.local` doesn't resolve, find the Pi's IP in your router's client
list and use `ssh <user>@<ip>`.

Optional entry in `~/.ssh/config` on the Mac, so plain `ssh bigair-ha` works:

```
Host bigair-ha bigair-ha.local
    HostName bigair-ha.local
    User <user>
    IdentityFile ~/.ssh/<the key Imager used>
    IdentitiesOnly yes
```

### sudo asks for a password (trixie)

On Raspberry Pi OS based on trixie, the user Imager creates needs its
password for `sudo`. The old `pi` user's passwordless sudo is gone. You can
run the bootstrap interactively (`ssh -t`) and type it when asked, or allow
passwordless sudo once. That's reasonable here because SSH accepts only your
key. Run it from the Mac's Terminal:

```bash
ssh -t bigair-ha 'echo "<user> ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/010-<user>-nopasswd >/dev/null \
  && sudo chmod 440 /etc/sudoers.d/010-<user>-nopasswd && sudo visudo -c'
```

It should end with `parsed OK`. Passwordless sudo is also what lets Claude
Code run the bootstrap over SSH.

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

**GPS:** set `GPS_BT_MAC` and `GPS_BT_CHANNEL` in `deploy/.env` after pairing
the receiver, and the bootstrap sets up gpsd on the host and checks for a fix
(`docs/hardware/gps-xgps160-trial.md`, step 6). It also points chrony at the
GPS, so the clock stays right offline. Check with `chronyc sources`: `GPS` is
listed, and it becomes the `*` reference when there's no internet. With `GPS_BT_MAC` empty, it
skips GPS. The bigair service isn't started yet; it's a Compose profile that
waits for the M1 services.

## 4. After the bootstrap (in a browser)

| What | URL | First step |
|---|---|---|
| Home Assistant | `http://bigair-ha.local:8123` | Create your account, then **Settings → Devices & services → Add integration → MQTT**, broker `localhost`, port `1883`, no username yet (issue #19). HA uses host networking, so it's `localhost`, not `mosquitto`. |
| Grafana | `http://bigair-ha.local:3000` | User `admin`; password from `grep GRAFANA_ADMIN_PASSWORD deploy/.env` on the Pi. Check **BigAir** folder → *Engine history* and *MPG map*. They'll be empty until data flows. |
| InfluxDB | `http://bigair-ha.local:8086` | User `admin`; password from `grep INFLUX_ADMIN_PASSWORD deploy/.env`. Rarely needed. |

Then install the **Home Assistant Companion app** on your iPhone and connect it
to `http://bigair-ha.local:8123` while on the same Wi-Fi.

## Updating

```bash
cd ~/BigAirHomeAssistant && git pull && ./deploy/bootstrap.sh
```

## The van Wi-Fi (`bigair`)

A router in the van provides SSID **`bigair`** (CLAUDE.md Decision #31). The
Pi keeps both profiles; the bootstrap never touches networking. Save the van
profile **without switching to it** (works even when the router is off):

```bash
sudo nmcli connection add type wifi ifname wlan0 con-name bigair ssid bigair \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk '<password>' \
  connection.autoconnect yes connection.autoconnect-priority 10
nmcli -f NAME,AUTOCONNECT,AUTOCONNECT-PRIORITY connection show
```

Priority 10 makes `bigair` win when both networks are in range at
(re)connect, so everything is on the van network together (WiCAN, LilyGo,
phone). NetworkManager doesn't roam away from a working connection on its
own. To move now: `sudo nmcli connection up bigair`.

The password lives only in NetworkManager on the Pi and in eink-screen's
gitignored `config.h`. Never put it in this repo, which is public.

## Tailscale (remote access)

Set `TAILSCALE_ENABLE=true` in `deploy/.env` and run the bootstrap. It
installs Tailscale and, without `TAILSCALE_AUTHKEY`, prints the command to
log in:

```bash
sudo tailscale up --ssh --accept-dns=false --hostname bigair-ha   # opens a login URL
```

MagicDNS is off on purpose (Decision #30): Docker's DNS and offline running
must not depend on tailscaled.

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

Once `ssh <user>@bigair-ha.local` works from the Mac without a password, Claude
Code can run these steps over SSH from the Mac. It confirms before each step
that changes the Pi.
