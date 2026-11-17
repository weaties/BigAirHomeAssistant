# GPS trial: Dual XGPS160 (SkyPro) over Bluetooth

Dan already owns a Dual XGPS160. Before buying a USB receiver, find out whether
it can be the van's GPS (track points every 5 s) and clock source (chrony via
gpsd, CLAUDE.md Decision #24 and issue #22). **Status: UNVERIFIED.** Nothing
below has been run on the Pi yet.

## What we know going in

- It sends NMEA over **Bluetooth Serial Port Profile (SPP)**. The USB port
  is **charge-only**; the manual describes no USB data mode.
- Dual's listing claims Linux support, but no Pi or gpsd setup is documented.
- A 2023 Linux kernel bug report (bugzilla.kernel.org #217663) from a pilot
  with this module says the SPP profile wasn't exposed on their Linux setup.
  It was never resolved.
- It's a deviation from the brief's "USB GPS preferred", and the Pi 5's single
  Bluetooth radio is shared with Home Assistant's Victron BLE scanning.

Fallback if the trial fails: a u-blox M8 USB receiver with an active antenna
(e.g. GNSS Store NEO-M8N USB kit).

## Prerequisites

- Pi 5 on Raspberry Pi OS, Bluetooth enabled; `bluez`, `gpsd`, `gpsd-clients`
  installed on the **host**.
- XGPS160 charged, on USB power, with sky view (dash or outside).
- Nothing else paired to the XGPS160 (an iPad holding the connection blocks
  the Pi). Turn Bluetooth off on other devices first.
- Stop any host gpsd that auto-starts: `sudo systemctl stop gpsd.socket gpsd`.

`rfcomm` and `sdptool` are deprecated BlueZ tools. If they're missing or
`sdptool` errors with "Failed to connect to SDP server", see the notes under
step 2.

## Steps

### 1. Pair and trust

```bash
bluetoothctl
  power on
  scan on            # wait for "XGPS160-xxxxxx", note its MAC
  scan off
  pair  <MAC>        # accept any PIN prompt (try 0000 / 1234 if asked)
  trust <MAC>
  info  <MAC>        # UUIDs should include "Serial Port (00001101-...)"
  quit
```

**Pass:** pairs, and `info` lists the Serial Port UUID. If there's no Serial
Port UUID, that's the kernel bug #217663 symptom: stop and record it.

### 2. Find the SPP channel and bind it

```bash
sdptool browse <MAC> | grep -A8 "Serial Port"    # note "Channel: N"
sudo rfcomm bind 0 <MAC> <N>                     # creates /dev/rfcomm0
ls -l /dev/rfcomm0
```

- If `sdptool` can't reach the SDP server, try channel 1 directly. That's the
  common default for single-service SPP devices, but it's unverified for this
  one.
- `rfcomm bind` doesn't connect until something opens the device.

### 3. Raw NMEA check

```bash
sudo cat /dev/rfcomm0          # Ctrl-C after a few seconds
```

**Pass:** `$GPRMC` / `$GPGGA` (or `$GN…`) sentences scroll once per second.

### 4. gpsd and a real fix

```bash
sudo gpsd -N -D2 -n /dev/rfcomm0      # foreground, debug level 2
cgps                                   # in a second terminal
```

**Pass:** `cgps` shows a 3D fix with a plausible lat/lon. Watch out for a fix
"mode 3D" alongside lat/lon 0.0, which is the failure reported for some USB
pucks. Note the time to first fix (cold, then warm).

### 5. Robustness (the tests that decide it)

Run each one and record pass/fail:

| # | Test | Pass |
|---|---|---|
| a | Reboot the Pi; re-run `rfcomm bind` + gpsd | Fix returns without re-pairing |
| b | Power-cycle the XGPS160 while gpsd runs | gpsd reconnects on its own within 1 min |
| c | HA running with Victron BLE scanning active, for 1 h | No GPS dropouts > 10 s |
| d | XGPS160 on USB power in the van for 24 h | Stays on (no idle auto-off), no battery issue |
| e | Clock: chrony with gpsd SHM refclock (issue #22), Pi offline | `chronyc sources` selects GPS; offset < 100 ms |
| f | Drive with it on the dash | Continuous track, sane speed vs dash |

### 6. Make it permanent (only if 1–5 pass)

The binding has to exist before the gpsd container starts, because Compose
maps the device at container start. Sketch of a host unit (**untested**):

```ini
# /etc/systemd/system/rfcomm-gps.service
[Unit]
Description=Bind XGPS160 SPP to /dev/rfcomm0
After=bluetooth.service
Requires=bluetooth.service
Before=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/rfcomm bind 0 <MAC> <N>
ExecStop=/usr/bin/rfcomm release 0

[Install]
WantedBy=multi-user.target
```

Then set `GPS_DEVICE=/dev/rfcomm0` in `deploy/.env`.

## Record the result

1. Write `docs/scans/YYYY-MM-DD-xgps160-trial.md` with the pass/fail table,
   time to first fix, and the channel number. Don't include the MAC or any
   positions.
2. Update the GPS row in `docs/pid-register.md` → Devices.
3. Tick the GPS item in issue #20. If it's adopted, add a CLAUDE.md Decision
   ("Bluetooth GPS instead of USB, because …"). If it's rejected, note why and
   order the USB fallback.
