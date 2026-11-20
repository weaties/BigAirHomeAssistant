#!/usr/bin/env python3
"""Write Pi power debug readings for the dash display (drive testing).

Every 5 s: the Pi 5 PMIC's 5 V input (vcgencmd pmic_read_adc EXT5V_V), the
throttle/under-voltage flags (vcgencmd get_throttled), uptime, and the van
battery voltage if something fresh wrote /run/bigair/van_battery_v. Written
atomically to /run/bigair/debug.json, which the display container mounts
read-only (DISPLAY_DEBUG_FILE). Stdlib only; runs as a host systemd service.
"""

import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime

OUT_DIR = "/run/bigair"
OUT = os.path.join(OUT_DIR, "debug.json")
VAN_FILE = os.path.join(OUT_DIR, "van_battery_v")
VAN_MAX_AGE_S = 30


def vcgencmd(*args: str) -> str:
    try:
        return subprocess.run(
            ["vcgencmd", *args], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def read() -> dict[str, object]:
    m = re.search(r"=([\d.]+)V", vcgencmd("pmic_read_adc", "EXT5V_V"))
    t = re.search(r"throttled=(0x[0-9a-fA-F]+)", vcgencmd("get_throttled"))
    with open("/proc/uptime") as f:
        uptime = float(f.read().split()[0])
    van = None
    try:
        if time.time() - os.path.getmtime(VAN_FILE) < VAN_MAX_AGE_S:
            with open(VAN_FILE) as f:
                van = float(f.read().strip())
    except (OSError, ValueError):
        pass
    return {
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pi_5v_v": float(m.group(1)) if m else None,
        "throttled": t.group(1) if t else "0x0",
        "van_battery_v": van,
        "uptime_s": round(uptime),
    }


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    while True:
        tmp = OUT + ".tmp"
        with open(tmp, "w") as f:
            json.dump(read(), f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, OUT)
        time.sleep(5)


if __name__ == "__main__":
    main()
