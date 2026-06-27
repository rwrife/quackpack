"""Unit tests for the recent-pipe log backing ``quackpack pipe``.

These exercise the pure data layer (``quackpack.pipes``) in isolation: query
fingerprinting (so cosmetic differences collapse), the bounded newest-first
log, recrecord/count semantics, and graceful degradation on a missing or
corrupt sidecar. The CLI-level behaviour lives in ``test_cli_pipe.py``.
"""

from __future__ import annotations

from pathlib import Path

from quackpack.pipes import MAX_PIPES, PipeLog, fingerprint


# --------------------------------------------------------------------------
# fingerprint
# --------------------------------------------------------------------------


def test_fingerprint_collapses_whitespace_and_case() -> None:
    a = fingerprint("SELECT *   FROM t")
    b = fingerprint("select *\nfrom   t")
    assert a == b == "select * from t"


def test_fingerprint_strips_trailing_semicolons() -> None:
    assert fingerprint("select 1 ;") == fingerprint("select 1") == "select 1"


def test_fingerprint_keeps_distinct_queries_distinct() -> None:
    assert fingerprint("select 1") != fingerprint("select 2")


# --------------------------------------------------------------------------
# record / count
# --------------------------------------------------------------------------


def test_first_record_has_count_one() -> None:
    log = PipeLog(path=Path("/does/not/matter"))
    entry = log.record("select 1")
    assert entry.count == 1
    assert log.count_for("select 1") == 1


def test_repeat_record_bumps_count_and_ignores_formatting() -> None:
    log = PipeLog(path=Path("/x"))
    log.record("select 1")
    entry = log.record("SELECT   1 ;")  # same query, different spelling
    assert entry.count == 2
    assert log.count_for("select 1") == 2
    assert len(log) == 1  # collapsed into one entry, not two


def test_record_moves_entry_to_front() -> None:
    log = PipeLog(path=Path("/x"))
    log.record("select 1")
    log.record("select 2")
    log.record("select 1")  # touch the older one again
    # Newest-first: the just-recorded query leads.
    assert log.find("select 1") is not None
    first = next(iter(log._entries))  # noqa: SLF001 - inspecting order in test
    assert first.fingerprint == fingerprint("select 1")


def test_count_for_unseen_is_zero() -> None:
    log = PipeLog(path=Path("/x"))
    assert log.count_for("select 99") == 0


def test_log_is_bounded() -> None:
    log = PipeLog(path=Path("/x"))
    for i in range(MAX_PIPES + 10):
        log.record(f"select {i}")
    log.save = lambda: None  # type: ignore[method-assign]  # guard against IO
    # Recording trims in-memory immediately.
    assert len(log) == MAX_PIPES


# --------------------------------------------------------------------------
# persistence + resilience
# --------------------------------------------------------------------------


def test_roundtrip_through_disk(tmp_path: Path) -> None:
    p = tmp_path / "pipes.json"
    log = PipeLog(path=p)
    log.record("select a")
    log.record("select b")
    log.save()

    reloaded = PipeLog.load(p)
    assert len(reloaded) == 2
    assert reloaded.count_for("select a") == 1
    assert reloaded.count_for("select b") == 1


def test_missing_file_loads_empty(tmp_path: Path) -> None:
    log = PipeLog.load(tmp_path / "nope.json")
    assert len(log) == 0


def test_corrupt_file_degrades_to_empty(tmp_path: Path) -> None:
    p = tmp_path / "pipes.json"
    p.write_text("{ this is not json", encoding="utf-8")
    log = PipeLog.load(p)
    assert len(log) == 0  # best-effort: never raises on bad sidecar


def test_save_caps_persisted_entries(tmp_path: Path) -> None:
    p = tmp_path / "pipes.json"
    log = PipeLog(path=p)
    for i in range(MAX_PIPES + 5):
        log.record(f"select {i}")
    log.save()
    reloaded = PipeLog.load(p)
    assert len(reloaded) == MAX_PIPES
