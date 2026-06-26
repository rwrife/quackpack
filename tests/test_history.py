"""Unit tests for :mod:`quackpack.history` — the pure run-history helpers.

These cover the timestamp parsing, age humanisation, and the combined
"last run" descriptor used by ``ls``/``show``. Everything here is deterministic:
a fixed ``now`` is injected so the relative-age math never depends on the wall
clock.
"""

from __future__ import annotations

import datetime as dt

import pytest

from quackpack.history import (
    ERROR,
    OK,
    RunHistory,
    describe_last_run,
    humanize_age,
    now_iso,
    parse_iso,
)

BASE = dt.datetime(2026, 6, 25, 12, 0, 0, tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------
# parse_iso
# --------------------------------------------------------------------------


def test_parse_iso_blank_and_garbage_return_none() -> None:
    assert parse_iso("") is None
    assert parse_iso("not-a-timestamp") is None


def test_parse_iso_roundtrips_now_iso() -> None:
    stamp = now_iso()
    parsed = parse_iso(stamp)
    assert parsed is not None
    assert parsed.tzinfo is not None  # tz-aware


def test_parse_iso_assumes_utc_for_naive() -> None:
    parsed = parse_iso("2026-06-25T12:00:00")  # no offset
    assert parsed is not None
    assert parsed.tzinfo == dt.timezone.utc


# --------------------------------------------------------------------------
# humanize_age
# --------------------------------------------------------------------------


def test_humanize_age_never_for_blank() -> None:
    assert humanize_age("") == "never"
    assert humanize_age("garbage") == "never"


@pytest.mark.parametrize(
    "stamp, expected",
    [
        ("2026-06-25T11:59:30+00:00", "just now"),  # 30s
        ("2026-06-25T11:30:00+00:00", "30m ago"),
        ("2026-06-25T11:00:00+00:00", "1h ago"),
        ("2026-06-25T00:00:00+00:00", "12h ago"),
        ("2026-06-23T12:00:00+00:00", "2d ago"),
        ("2026-06-04T12:00:00+00:00", "3w ago"),
        ("2025-06-25T12:00:00+00:00", "1y ago"),
    ],
)
def test_humanize_age_units(stamp: str, expected: str) -> None:
    assert humanize_age(stamp, now=BASE) == expected


def test_humanize_age_future_clamps_to_just_now() -> None:
    # Clock skew / hand-edited future timestamp must not print a negative age.
    future = "2026-06-25T13:00:00+00:00"
    assert humanize_age(future, now=BASE) == "just now"


# --------------------------------------------------------------------------
# describe_last_run
# --------------------------------------------------------------------------


def test_describe_last_run_never() -> None:
    assert describe_last_run("", "") == "never"
    # Even with a leftover status, a blank timestamp reads as never.
    assert describe_last_run("", ERROR) == "never"


def test_describe_last_run_ok_has_no_status_suffix() -> None:
    out = describe_last_run("2026-06-25T10:00:00+00:00", OK, now=BASE)
    assert out == "2h ago"


def test_describe_last_run_error_is_flagged() -> None:
    out = describe_last_run("2026-06-25T10:00:00+00:00", ERROR, now=BASE)
    assert out == "2h ago (error)"


# --------------------------------------------------------------------------
# RunHistory
# --------------------------------------------------------------------------


def test_runhistory_record_increments_and_stamps() -> None:
    h = RunHistory()
    assert h.run_count == 0 and h.last_run == "" and h.last_status == ""
    h2 = h.record(OK, when="2026-06-25T12:00:00+00:00")
    assert h2.run_count == 1
    assert h2.last_run == "2026-06-25T12:00:00+00:00"
    assert h2.last_status == OK
    # Original is untouched (record returns a fresh instance).
    assert h.run_count == 0


def test_runhistory_record_accumulates() -> None:
    h = RunHistory().record(OK).record(ERROR).record(OK)
    assert h.run_count == 3
    assert h.last_status == OK
