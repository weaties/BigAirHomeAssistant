# Mac copy of the planner, and HA through the Mac (#105)

When the van router uses the phone's hotspot as its uplink, the phone can't reach
the Pi over Tailscale. The Pi's public endpoint is the phone's own carrier NAT, and
the phone can't hairpin through itself (#104). A Mac on another network (at home, or
anywhere else with internet) can reach both, so it stands in:

| On the Mac (tailnet address) | What it is |
|---|---|
| `:8092` | its own `bigair planner`, reading the tank from the Pi's Mosquitto |
| `:8123` | a byte relay to the Pi's Home Assistant |
| `:3000` | a byte relay to the Pi's Grafana |

It's a convenience only. Nothing on the Pi depends on it, and the van works without it.

## Set up

```bash
cp deploy/mac/env.mac.example deploy/.env.mac   # fill in PI_TS_IP, MAC_TS_IP, POI_DIR
deploy/mac/setup.sh --copy-keys --copy-data     # keys and map data from the Pi over SSH
deploy/mac/setup.sh --remove                    # take it all down
```

The script installs three launchd agents (`com.bigair.planner`, `com.bigair.relay-ha`,
`com.bigair.relay-grafana`). They start at login, restart if they die, and log to
`~/Library/Logs/bigair/`. Run it from the checkout you want the planner to use.
Re-running it after a `git pull` picks up new code.

## Access without Tailscale (Funnel)

For someone who isn't on the tailnet, the Mac publishes **only Home Assistant** with
Tailscale Funnel, at `https://<mac>.<tailnet>.ts.net/`. HA's own login is the gate. The
planner and Grafana have no login, so they stay tailnet-only: a public planner would
let anyone spend the Google and Claude budgets, and Grafana shows the GPS track.

```bash
/Applications/Tailscale.app/Contents/MacOS/Tailscale funnel --bg --yes http://127.0.0.1:8123
/Applications/Tailscale.app/Contents/MacOS/Tailscale funnel --https=443 off   # stop
```

Funnel targets `127.0.0.1`, because the macOS Tailscale extension can't proxy to the
Mac's own tailnet address. The relay listens on both. The Funnel setting survives
restarts, but the URL only answers while the Mac is awake and online.

HA must trust the Mac as a proxy, or it rejects Funnel's `X-Forwarded-For` with a 400.
**Since HA 2026.11 the `http:` settings live in `.storage/http`, not
`configuration.yaml`** (`yaml_migration_done`), so an `http:` block in YAML is
ignored. With HA stopped, set these in `data.stable`:

```json
"use_x_forwarded_for": true,
"trusted_proxies": ["<the Mac's tailnet address>"],
"ip_ban_enabled": true,
"login_attempts_threshold": 5
```

Five failed logins from one address ban it, and bans land in `ip_bans.yaml` in HA's
config folder. Each person gets their own non-admin HA user with two-factor login
turned on, created in HA by the owner.

## Differences from the Pi's planner

- **No "current location".** gpsd listens only on the Pi's localhost
  (Decision #28), so the Mac copy waits 5 s for a fix, then gives up. Type the start
  instead.
- **The evening briefing stays off.** The Pi sends it; two senders would mean two
  notifications.
- **Separate counters.** The daily plan limit and Claude's spend ledger are the
  Mac's own, so `CLAUDE_MONTHLY_CAP_USD` defaults to $5 here. The Console spend limit
  still caps both.
- **POIs come straight from the vault folder**, so Syncthing isn't needed on the Mac.
- **HA's Trip view** still embeds `bigair-ha.local:8092`, which only works on the van
  Wi-Fi. Open the Mac's `:8092` directly instead.

## Why a Python relay, not socat

The macOS application firewall silently drops inbound connections to a newly
installed binary like socat. The uv-managed Python is already allowed, because the
planner runs under it, so `deploy/mac/relay.py` (stdlib asyncio) runs under it too. That
way no firewall setting changes.
