"""Integration tests for the M3 ``run`` command (and render formats).

Drives the Typer CLI end to end against a throwaway ``QUACKPACK_HOME`` and tiny
on-disk data fixtures, covering table/csv/json output, ``:param`` binding, the
SQLite fallback engine, and the user-facing error paths.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quackpack import engine
from quackpack.cli import app

runner = CliRunner()

needs_duckdb = pytest.mark.skipif(
    not engine.DUCKDB_AVAILABLE, reason="duckdb not installed"
)


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("QUACKPACK_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def sales_csv(tmp_path: Path) -> Path:
    p = tmp_path / "sales.csv"
    p.write_text("region,amount\nwest,100\neast,250\nwest,75\n", encoding="utf-8")
    return p


@pytest.fixture
def sales_parquet(tmp_path: Path, sales_csv: Path) -> Path:
    duckdb = pytest.importorskip("duckdb")
    out = tmp_path / "sales.parquet"
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE sales AS SELECT * FROM read_csv_auto(?)", [str(sales_csv)])
    con.execute(f"COPY sales TO '{out}' (FORMAT parquet)")
    con.close()
    return out


def _add(name: str, sql: str) -> None:
    res = runner.invoke(app, ["add", "-n", name, "-q", sql])
    assert res.exit_code == 0, res.stdout


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------


@needs_duckdb
def test_run_csv_table(sales_csv: Path) -> None:
    _add("agg", "select region, sum(amount) as total from sales group by region")
    res = runner.invoke(app, ["run", "agg", "--file", str(sales_csv)])
    assert res.exit_code == 0, res.stdout
    out = res.stdout
    assert "region" in out and "total" in out
    assert "east" in out and "west" in out
    assert "rows" in out  # footer


@needs_duckdb
def test_run_parquet_csv_format(sales_parquet: Path) -> None:
    _add("all", "select region, amount from sales order by amount")
    res = runner.invoke(app, ["run", "all", "--file", str(sales_parquet), "-F", "csv"])
    assert res.exit_code == 0, res.stdout
    lines = [ln for ln in res.stdout.strip().splitlines() if ln]
    assert lines[0] == "region,amount"
    assert "east,250" in lines


@needs_duckdb
def test_run_json_format_with_param(sales_csv: Path) -> None:
    _add("big", "select region, amount from sales where amount > :min order by amount")
    res = runner.invoke(
        app, ["run", "big", "--file", str(sales_csv), "-p", "min=90", "-F", "json"]
    )
    assert res.exit_code == 0, res.stdout
    data = json.loads(res.stdout)
    assert data == [
        {"region": "west", "amount": 100},
        {"region": "east", "amount": 250},
    ]


def test_run_sqlite_engine(sales_csv: Path) -> None:
    _add("agg", "select region, sum(amount) as total from sales group by region order by region")
    res = runner.invoke(
        app, ["run", "agg", "--file", str(sales_csv), "--engine", "sqlite", "-F", "csv"]
    )
    assert res.exit_code == 0, res.stdout
    out = res.stdout
    assert "east,250" in out
    assert "west,175" in out


def test_run_sqlite_numeric_param(sales_csv: Path) -> None:
    _add("big", "select amount from sales where amount > :min order by amount")
    res = runner.invoke(
        app,
        ["run", "big", "--file", str(sales_csv), "-p", "min=90", "-e", "sqlite", "-F", "csv"],
    )
    assert res.exit_code == 0, res.stdout
    body = [ln for ln in res.stdout.strip().splitlines() if ln]
    assert body == ["amount", "100", "250"]


# --------------------------------------------------------------------------
# Error / edge paths
# --------------------------------------------------------------------------


def test_run_unknown_query() -> None:
    res = runner.invoke(app, ["run", "ghost"])
    assert res.exit_code == 1
    assert "No query named" in res.stderr


def test_run_missing_file(sales_csv: Path) -> None:
    _add("q", "select 1")
    res = runner.invoke(app, ["run", "q", "--file", str(sales_csv.parent / "nope.csv")])
    assert res.exit_code == 1
    assert "not found" in res.stderr


def test_run_bad_param_format(sales_csv: Path) -> None:
    _add("q", "select 1 as one")
    res = runner.invoke(app, ["run", "q", "-p", "noequals"])
    assert res.exit_code == 1
    assert "key=value" in res.stderr


def test_run_bad_format_rejected() -> None:
    _add("q", "select 1 as one")
    res = runner.invoke(app, ["run", "q", "-F", "yaml"])
    assert res.exit_code == 1
    assert "format" in res.stderr.lower()


def test_run_sql_error(sales_csv: Path) -> None:
    _add("broken", "select * from nope_table")
    res = runner.invoke(app, ["run", "broken", "--file", str(sales_csv), "-e", "sqlite"])
    assert res.exit_code == 1
    assert "SQL error" in res.stderr


def test_run_missing_param_warns_but_attempts(sales_csv: Path) -> None:
    # Query declares :min but none is passed -> warning on stderr; engine then
    # raises a binding/SQL error (non-zero exit).
    _add("needs", "select amount from sales where amount > :min")
    res = runner.invoke(app, ["run", "needs", "--file", str(sales_csv)])
    assert "warning" in res.stderr.lower()
    assert res.exit_code == 1
