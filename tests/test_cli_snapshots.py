"""Integration tests for result snapshots & diff (backlog #3).

Drives the real Typer app end to end against a throwaway ``QUACKPACK_HOME`` and
CSV fixtures, covering:

* ``run`` caches a snapshot (and ``--no-snapshot`` opts out);
* ``run --key`` records identity columns so ``diff`` can report *changed* rows;
* ``diff`` shows added / removed / changed rows, reports "no changes" when the
  result is identical, and errors cleanly when no snapshot exists yet;
* ``diff --update`` re-baselines the snapshot;
* ``diff --key`` overrides the stored identity;
* ``snapshot show`` / ``snapshot rm`` inspect and clear the cache.

Everything routes through DuckDB (skipped if unavailable); a CSV named
``sales.csv`` auto-derives the ``sales`` relation the SQL references.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from quackpack import engine
from quackpack.cli import app
from quackpack.snapshots import load_snapshot, snapshot_path

runner = CliRunner()

needs_duckdb = pytest.mark.skipif(
    not engine.DUCKDB_AVAILABLE, reason="duckdb not installed"
)

SQL = "select id, region, amount from sales order by id"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("QUACKPACK_HOME", str(tmp_path))
    return tmp_path


def _write_sales(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "sales.csv"
    p.write_text(body, encoding="utf-8")
    return p


def _add(name: str = "sales", sql: str = SQL) -> None:
    res = runner.invoke(app, ["add", "-n", name, "-q", sql])
    assert res.exit_code == 0, res.stdout


def _run(csv: Path, *extra: str, name: str = "sales") -> None:
    res = runner.invoke(app, ["run", name, "--file", str(csv), *extra])
    assert res.exit_code == 0, res.stdout


# --------------------------------------------------------------------------
# run caches a snapshot
# --------------------------------------------------------------------------


@needs_duckdb
def test_run_creates_snapshot(tmp_path: Path) -> None:
    csv = _write_sales(tmp_path, "id,region,amount\n1,west,100\n2,east,250\n")
    _add()
    _run(csv, "--key", "id")

    snap = load_snapshot("sales")
    assert snap is not None
    assert snap.key == ["id"]
    assert snap.rowcount == 2
    assert snap.columns == ["id", "region", "amount"]


@needs_duckdb
def test_run_no_snapshot_flag_skips_cache(tmp_path: Path) -> None:
    csv = _write_sales(tmp_path, "id,region,amount\n1,west,100\n")
    _add()
    _run(csv, "--no-snapshot")
    assert load_snapshot("sales") is None
    assert not snapshot_path("sales").exists()


# --------------------------------------------------------------------------
# diff — happy paths
# --------------------------------------------------------------------------


@needs_duckdb
def test_diff_no_changes(tmp_path: Path) -> None:
    csv = _write_sales(tmp_path, "id,region,amount\n1,west,100\n2,east,250\n")
    _add()
    _run(csv, "--key", "id")
    res = runner.invoke(app, ["diff", "sales", "--file", str(csv)])
    assert res.exit_code == 0, res.stdout
    assert "no changes" in res.stdout.lower()


@needs_duckdb
def test_diff_keyed_added_removed_changed(tmp_path: Path) -> None:
    csv = _write_sales(tmp_path, "id,region,amount\n1,west,100\n2,east,250\n3,west,75\n")
    _add()
    _run(csv, "--key", "id")
    # Modify id=2 amount, drop id=3, add id=4.
    _write_sales(tmp_path, "id,region,amount\n1,west,100\n2,east,999\n4,north,50\n")

    res = runner.invoke(app, ["diff", "sales", "--file", str(csv)])
    assert res.exit_code == 0, res.stdout
    out = res.stdout
    assert "+1 added, -1 removed, ~1 changed" in out
    # The changed row shows the amount transition.
    assert "250" in out and "999" in out


@needs_duckdb
def test_diff_unkeyed_added_removed_only(tmp_path: Path) -> None:
    csv = _write_sales(tmp_path, "id,region,amount\n1,west,100\n2,east,250\n")
    _add()
    _run(csv)  # no key -> whole-row identity
    _write_sales(tmp_path, "id,region,amount\n1,west,100\n3,west,75\n")

    res = runner.invoke(app, ["diff", "sales", "--file", str(csv)])
    assert res.exit_code == 0, res.stdout
    assert "+1 added, -1 removed, ~0 changed" in res.stdout


@needs_duckdb
def test_diff_key_override(tmp_path: Path) -> None:
    # Snapshot saved without a key; --key on diff enables change detection.
    csv = _write_sales(tmp_path, "id,region,amount\n1,west,100\n")
    _add()
    _run(csv)
    _write_sales(tmp_path, "id,region,amount\n1,west,555\n")

    res = runner.invoke(app, ["diff", "sales", "--file", str(csv), "--key", "id"])
    assert res.exit_code == 0, res.stdout
    assert "~1 changed" in res.stdout
    assert "100" in res.stdout and "555" in res.stdout


@needs_duckdb
def test_diff_update_rebaselines(tmp_path: Path) -> None:
    csv = _write_sales(tmp_path, "id,region,amount\n1,west,100\n")
    _add()
    _run(csv, "--key", "id")
    _write_sales(tmp_path, "id,region,amount\n1,west,200\n")

    # First diff --update should show a change AND refresh the snapshot.
    res = runner.invoke(app, ["diff", "sales", "--file", str(csv), "--update"])
    assert res.exit_code == 0, res.stdout
    assert "~1 changed" in res.stdout
    assert "snapshot updated" in res.stdout.lower()

    # Second diff (same data) is now clean.
    res2 = runner.invoke(app, ["diff", "sales", "--file", str(csv)])
    assert res2.exit_code == 0, res2.stdout
    assert "no changes" in res2.stdout.lower()

    snap = load_snapshot("sales")
    assert snap is not None and snap.rows == [(1, "west", 200)]


# --------------------------------------------------------------------------
# diff — error paths
# --------------------------------------------------------------------------


@needs_duckdb
def test_diff_without_snapshot_errors(tmp_path: Path) -> None:
    csv = _write_sales(tmp_path, "id,region,amount\n1,west,100\n")
    _add()
    res = runner.invoke(app, ["diff", "sales", "--file", str(csv)])
    assert res.exit_code == 1
    assert "no snapshot" in res.stderr.lower()


def test_diff_unknown_query_errors() -> None:
    res = runner.invoke(app, ["diff", "ghost", "--file", "x.csv"])
    assert res.exit_code == 1
    assert "no query named" in res.stderr.lower()


@needs_duckdb
def test_diff_bad_key_errors(tmp_path: Path) -> None:
    csv = _write_sales(tmp_path, "id,region,amount\n1,west,100\n")
    _add()
    _run(csv)
    res = runner.invoke(app, ["diff", "sales", "--file", str(csv), "--key", "nope"])
    assert res.exit_code == 1
    assert "not found" in res.stderr.lower()


# --------------------------------------------------------------------------
# snapshot show / rm
# --------------------------------------------------------------------------


@needs_duckdb
def test_snapshot_show(tmp_path: Path) -> None:
    csv = _write_sales(tmp_path, "id,region,amount\n1,west,100\n2,east,250\n")
    _add()
    _run(csv, "--key", "id")
    res = runner.invoke(app, ["snapshot", "show", "sales"])
    assert res.exit_code == 0, res.stdout
    assert "2 rows" in res.stdout
    # metadata line mentions the key
    assert "id" in res.stdout


@needs_duckdb
def test_snapshot_show_json(tmp_path: Path) -> None:
    csv = _write_sales(tmp_path, "id,region,amount\n1,west,100\n")
    _add()
    _run(csv)
    res = runner.invoke(app, ["snapshot", "show", "sales", "--format", "json"])
    assert res.exit_code == 0, res.stdout
    assert '"region": "west"' in res.stdout


def test_snapshot_show_missing_errors() -> None:
    _add()
    res = runner.invoke(app, ["snapshot", "show", "sales"])
    assert res.exit_code == 1
    assert "no snapshot" in res.stderr.lower()


@needs_duckdb
def test_snapshot_rm(tmp_path: Path) -> None:
    csv = _write_sales(tmp_path, "id,region,amount\n1,west,100\n")
    _add()
    _run(csv)
    assert snapshot_path("sales").exists()

    res = runner.invoke(app, ["snapshot", "rm", "sales", "--yes"])
    assert res.exit_code == 0, res.stdout
    assert not snapshot_path("sales").exists()

    # Removing again is a friendly no-op, not an error.
    res2 = runner.invoke(app, ["snapshot", "rm", "sales", "--yes"])
    assert res2.exit_code == 0
    assert "no snapshot to remove" in res2.stdout.lower()


def test_snapshot_rm_missing_is_noop() -> None:
    res = runner.invoke(app, ["snapshot", "rm", "never", "--yes"])
    assert res.exit_code == 0
    assert "no snapshot to remove" in res.stdout.lower()
