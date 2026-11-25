# Vault POIs → the Pi with Syncthing (#88)

The trip planner reads Dan's road-trip points of interest: one Markdown note per
place, with `location:` frontmatter, from his Obsidian vault. Syncthing copies
that one folder from the Mac to the Pi, **one way**. The Pi never writes to the
vault. It works on the van LAN or over Tailscale, with no internet needed.

The notes are personal and this repo is public, so the Pi copy lives under
`data/` (gitignored), and only that POI folder is shared, not the whole vault.

## Pi

1. In `deploy/.env`, set `SYNCTHING_ENABLE=true`. Leave `POI_HOST_DIR=../data/poi`,
   which is `data/poi` in the repo.
2. Run `./deploy/bootstrap.sh`. It installs Debian's `syncthing`, enables
   `syncthing@<user>`, and creates the folder.
3. Open the Syncthing web UI. It listens on the Pi's localhost only:
   `ssh -L 8384:localhost:8384 bigair-ha` from the Mac, then
   <http://localhost:8384/>. Set a GUI password when it asks.

## Mac

1. Install Syncthing (`brew install --cask syncthing`) and open it.
2. **Add Remote Device** with the Pi's device ID (Pi web UI → Actions → Show ID).
   Accept the Mac on the Pi.
3. **Add Folder** pointing at the vault's POI folder (`…/road-trip-2027/map/poi`),
   with folder type **Send Only**, shared with the Pi.
4. On the Pi, accept the folder, set its path to the repo's `data/poi` and its
   type to **Receive Only**.

Edits in Obsidian reach the Pi within seconds whenever both are on the same
network or the tailnet. The planner reads the folder on every plan, so there's
nothing to restart.

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
