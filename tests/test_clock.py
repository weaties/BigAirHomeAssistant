"""System-clock sync guard for Influx writes (#22, Decision #24)."""

from __future__ import annotations

import sys

import pytest

from bigair.clock import TIME_ERROR, kernel_clock_synced, synced_from


def test_synced_when_the_kernel_says_ok() -> None:
    assert synced_from(state=0, status=0x2001, maxerror_us=34_000)


def test_unsync_flag_means_not_synced() -> None:
    assert not synced_from(state=0, status=0x0040, maxerror_us=34_000)


def test_time_error_state_means_not_synced() -> None:
    assert not synced_from(state=TIME_ERROR, status=0, maxerror_us=34_000)


def test_huge_error_bound_means_not_synced() -> None:
    # The kernel grows maxerror while no one disciplines the clock (16 s = gave up).
    assert not synced_from(state=0, status=0, maxerror_us=16_000_000)


@pytest.mark.skipif(sys.platform != "linux", reason="adjtimex is Linux-only")
def test_kernel_call_returns_a_bool_on_linux() -> None:
    assert isinstance(kernel_clock_synced(), bool)


def test_non_linux_is_never_considered_synced(monkeypatch: pytest.MonkeyPatch) -> None:
    import bigair.clock as clock

    monkeypatch.setattr(clock.sys, "platform", "darwin")
    assert not clock.kernel_clock_synced()
