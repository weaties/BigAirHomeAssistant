# Backups (#111)

Every night the Pi takes an encrypted snapshot of its data and configuration.
The Mac copies it whenever both are online. Retention is 7 daily, 4 weekly and
6 monthly snapshots.

```
 Pi: bigair-backup.timer (03:30, or at the next boot if the van was off)
     deploy/backup.sh ── stage ──► restic repo data/backup/restic (encrypted)
                                   + data/backup/status.json (ok / failed)
 Mac: com.bigair.backup-pull (hourly)
     status ok and newer? ── rsync ──► ~/Backups/bigair-restic (same encrypted repo)
```

## What's in a snapshot

| Path in the snapshot | What | How |
|---|---|---|
| `influx/` | InfluxDB: every bucket (van_raw, van_history) plus metadata and tokens | `influx backup` |
| `homeassistant/home-assistant_v2.db` | HA history (recorder) | SQLite online backup, safe while HA runs |
| `homeassistant/.storage/` | HA users, Companion app registrations, integrations, http settings (#105) | copy |
| `homeassistant/secrets.yaml`, `ip_bans.yaml` | HA secrets and bans | copy |
| `grafana/grafana.db` | Grafana users and settings (dashboards come from git) | SQLite online backup |
| `bigair/` | fuel state, Claude spend ledger, last plan, briefing, travel rules | copy |
| `deploy/.env`, `deploy/git-head` | every secret and key, and the commit that was running | copy |

Left out on purpose:
- everything in git;
- the POI notes (the Obsidian vault is the source, and Syncthing refills them);
- byways and public-land GeoJSON (re-downloadable);
- Docker images;
- Mosquitto's retained messages (display copies only, Decision #22).

**The snapshot holds every secret**, so it's encrypted with `RESTIC_PASSWORD`. The
password lives in the Pi's `deploy/.env` and in the Mac's `deploy/.env.mac`
(`deploy/mac/setup.sh --copy-keys` copies it). **Keep a copy in your password
manager too.** Without the password, the backup can't be read.

## Check on it

```bash
# Pi
systemctl list-timers bigair-backup.timer
cat ~/BigAirHomeAssistant/data/backup/status.json
journalctl -u bigair-backup.service -n 20
sudo ~/BigAirHomeAssistant/deploy/backup.sh       # run one now

# Mac
tail ~/Library/Logs/bigair/com.bigair.backup-pull.log
cat ~/Backups/bigair-restic.status.json
RESTIC_PASSWORD=… restic -r ~/Backups/bigair-restic snapshots
```

The Mac pulls over `ssh bigair-ha`. At home that's the LAN with a key. Over the
tailnet, Tailscale SSH may want its periodic browser check, and until then the pull
just skips. The log says so.

## Restore

From the Mac's copy (or from `data/backup/restic` on the Pi):

```bash
export RESTIC_PASSWORD=…   # from deploy/.env.mac or your password manager
restic -r ~/Backups/bigair-restic snapshots
restic -r ~/Backups/bigair-restic restore latest --target /tmp/bigair-restore
# Files land under /tmp/bigair-restore/<the Pi's staging path>/…
```

On a fresh Pi, after `deploy/bootstrap.sh` has created the stack:

1. Stop the stack: `sudo docker compose -f deploy/docker-compose.yml down`.
2. Put back `deploy/.env` first. It holds the Influx admin token that the restore
   needs.
3. Copy `bigair/*` → `data/bigair/`, `homeassistant/.storage`, `secrets.yaml` and
   `home-assistant_v2.db` → `homeassistant/`, and `grafana/grafana.db` →
   `deploy/volumes/grafana/` (owned by uid 472).
4. Start InfluxDB alone (`… up -d influxdb`), copy `influx/` into the container, and
   run `influx restore --full /tmp/influx --token <INFLUX_ADMIN_TOKEN>`.
5. Start everything: `… up -d`. Check the fuel state log line ("fuel state restored:
   NN gal") against what you expect before trusting the tank.
