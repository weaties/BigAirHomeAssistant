---
name: can-sniffing
description: Workflow for finding non-standard signals (cruise control state and set speed, brake pedal, anything not exposed as an OBD PID) by capturing raw CAN with the WiCAN and SavvyCAN, correlating frames with deliberate driver actions, and recording a decoded signal as UNVERIFIED in docs/pid-register.md. TRIGGER when working on driver-input signals (cruise, brake), any CAN ID decoding, or planning a capture session. DO NOT trigger for standard OBD PIDs or UDS DIDs (use /pid-discovery).
---

# CAN sniffing — cruise, brake and other broadcast signals

Cruise state, cruise set speed and brake pedal are **not standard PIDs**.
No CAN ID for them is known for this van. Never copy one from another
Mercedes model or a forum without capturing it here.

## Know the bus before you sniff

- On the W906 the OBD diagnostic CAN (pins 6/14) is reported, from an EU
  OM651 van, to be **quiet behind the EZS gateway** until a tester talks
  (`mcgyver78/WiCAN_W906` docs). If that holds for our US OM642, the OBD port
  may show little or no broadcast traffic. The cruise and brake frames could
  then live only on the chassis or powertrain CAN, which needs a tap
  elsewhere (behind the cluster, or at the EZS). That's a hardware decision:
  stop and ask Dan before splicing anything.
- First capture = **passive listen, ignition on, engine running, no tester
  frames**. If it's silent, record that in the register as evidence. Don't
  start injecting.
- Listen-only mode. Never transmit onto a vehicle bus during a sniff
  session.

## Capture setup

- WiCAN in SLCAN/RealDash mode on pins 6/14 at 500 kbit/s (the bitrate is
  UNVERIFIED for our van; try autobaud first). Connect SavvyCAN over the
  network as an SLCAN/GVRET device. Check the current WiCAN docs for the
  port and mode names, because they change between firmware versions.
- Record with SavvyCAN logging to `scans/raw/YYYY-MM-DD-<topic>.csv`, which
  is gitignored.
- A second person drives. The sniffer operator calls out and timestamps each
  action, or use SavvyCAN's frame markers.

## Action script (repeat every action ≥ 5×, spaced ≥ 5 s apart)

1. Baseline: 30 s steady state, no inputs.
2. Brake: press / release (parked, engine on), then while rolling slowly.
3. Cruise: arm, set at about 45 mph, cancel. Resume. Set + ×3, set − ×3.
   Only on a safe, empty road.
4. Throttle sweep, as a cross-check against PID `49` if it is verified.

## Analysis

1. In SavvyCAN's Sniffer view, look for IDs whose bytes flip **only** at
   your marked times.
2. Brake: a bit that tracks the pedal 1:1. Cruise active: a bit that is set
   while cruise is engaged. Set speed: a byte or word that steps with
   set+/set−, so check km/h vs mph scaling against the dash readout.
3. Confirm on a **second capture on a different day** before RESPONDS.
   Promote to VERIFIED only after a full drive where the decoded signal
   matches the logged actions without a miss.

## Recording a signal

Add a row to the "Raw CAN broadcast signals" table in
`docs/pid-register.md`: bus/pins, ID, byte/bit position, scaling, status
and a link to `docs/scans/YYYY-MM-DD-<topic>.md`. The write-up holds the
action log, the matching frame excerpts (VIN-free) and the time
correlation. Then write a test-first decoder (`/tdd`) from those excerpts.

## Downstream uses (M2)

- Share of trip on cruise, brake events per mile, and mpg on cruise vs
  manual.
- These are only meaningful once the cruise and brake signals are VERIFIED.
  Until then, the dashboards show "signal unverified".
