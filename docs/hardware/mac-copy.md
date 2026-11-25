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
