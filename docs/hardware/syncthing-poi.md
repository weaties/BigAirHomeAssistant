# Vault POIs → the Pi with Syncthing (#88)

The trip planner reads Dan's road-trip points of interest: one Markdown note per
place, with `location:` frontmatter, from his Obsidian vault. Syncthing copies
that one folder from the Mac to the Pi, **one way**. The Pi never writes to the
vault. It works on the van LAN or over Tailscale, with no internet needed.

The notes are personal and this repo is public, so the Pi copy lives under
`data/` (gitignored), and only that POI folder is shared, not the whole vault.

## Setup (done on 2026-11-25)

Both ends run Syncthing as a background service: `syncthing@<user>` (systemd) on
the Pi, and `brew services` (a login agent) on the Mac.

- **Pi:** set `SYNCTHING_ENABLE=true` in `deploy/.env` and run the bootstrap.
  It installs Debian's `syncthing` with `--no-install-recommends`; without that
  flag, apt pulls about 400 MB of extras.
- **Mac:** `brew install syncthing && brew services start syncthing`. On Apple
  Silicon from a Rosetta shell, use `arch -arm64 brew …`.
- **Privacy:** global discovery, relays and NAT traversal are **off** on both
  ends, so nothing goes through syncthing.net. Each device lists the other by
  Tailscale address plus `dynamic`, so they find each other by local discovery
  on the same LAN, or over the tailnet.
- **Folder `rt27-poi`:** on the Mac, the vault's `…/road-trip-2027/map/poi` is
  **Send Only**, with `.stignore` = `(?d).DS_Store`. On the Pi, the repo's
  `data/poi` is **Receive Only**.

Both web UIs listen on localhost only (`ssh -L 8384:localhost:8384 bigair-ha`
for the Pi's). The Pi's Debian Syncthing is 1.x, and its `syncthing cli` crashes
on list-valued settings, so change those through the web UI or the REST API.

Edits in Obsidian reach the Pi within about 30 s. The planner reads the folder
on every plan, so there's nothing to restart.

## Check

- The planner page shows a **Points of interest near the route** card after a
  plan.
- A warning like `POI folder /poi not found` means the mount or Syncthing isn't
  set up. A note without frontmatter `location:` is listed as skipped. Neither
  stops the fuel plan.

## Planning fields

The planner uses these optional frontmatter keys. When one is missing it shows
"unknown"; it never guesses:

| Key | Meaning |
|---|---|
| `visit_min` | Minutes for a detour visit (not counting the drive to it) |
| `visit_what` | What that time covers |
| `best_months` | Months when it's at its best, e.g. `[4, 5, 9, 10]` |
| `destination` | `true` if it's somewhere to stay 3 nights |
| `rating` | Dan's own rating, 1–5 |
