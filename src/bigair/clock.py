"""Is the system clock trustworthy? (#22, Decision #24)

The Pi has no battery-backed clock and the van is often offline. chrony
(GPS refclock + NTP when online) disciplines the kernel clock and keeps the
kernel's sync status current, so reading it with adjtimex() (read-only, no
privileges, works inside the container) answers "may bigair write timestamps
to InfluxDB yet?". Until it says yes, the Influx writer buffers.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys

TIME_ERROR = 5  # adjtimex return: clock not synchronised
STA_UNSYNC = 0x0040
MAX_ERROR_US = 1_000_000  # >1 s error bound: not good enough for telemetry


def synced_from(state: int, status: int, maxerror_us: int) -> bool:
    return state != TIME_ERROR and not status & STA_UNSYNC and maxerror_us < MAX_ERROR_US


class _Timex(ctypes.Structure):
    # struct timex (linux/timex.h) on 64-bit; we only read the leading fields.
    _fields_ = [  # noqa: RUF012 — ctypes API
        ("modes", ctypes.c_uint),
        ("offset", ctypes.c_long),
        ("freq", ctypes.c_long),
        ("maxerror", ctypes.c_long),
        ("esterror", ctypes.c_long),
        ("status", ctypes.c_int),
        ("constant", ctypes.c_long),
        ("precision", ctypes.c_long),
        ("tolerance", ctypes.c_long),
        ("time_sec", ctypes.c_long),
        ("time_usec", ctypes.c_long),
        ("tick", ctypes.c_long),
        ("ppsfreq", ctypes.c_long),
        ("jitter", ctypes.c_long),
        ("shift", ctypes.c_int),
        ("stabil", ctypes.c_long),
        ("jitcnt", ctypes.c_long),
        ("calcnt", ctypes.c_long),
        ("errcnt", ctypes.c_long),
        ("stbcnt", ctypes.c_long),
        ("tai", ctypes.c_int),
        ("_pad", ctypes.c_int * 11),
    ]


def kernel_clock_synced() -> bool:
    """True if the kernel reports a synchronised clock. Never raises; False if unknown."""
    if sys.platform != "linux":
        return False
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        tx = _Timex()  # modes = 0: read only
        state = int(libc.adjtimex(ctypes.byref(tx)))
    except (OSError, AttributeError):
        return False
    if state < 0:
        return False
    return synced_from(state, tx.status, tx.maxerror)
