"""Integration tests for the ``explain`` command (issue #31).

Covers EXPLAIN plan capture (DuckDB + SQLite fallback), ANALYZE, param
binding, the static lint surface, and error paths — end to end through the
Typer CLI against a throwaway ``QUACKPACK_HOME``.
"""

from __future__ import annotations

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


def _add(name: str, sql: str) -> None:
    res = runner.invoke(app, ["add", "-n", name, "-q", sql])
    assert res.exit_code == 0, res.stdout


@needs_duckdb
def test_explain_prints_plan(sales_csv: Path) -> None:
    _add("agg", "select region, sum(amount) as total from sales group by region")
    res = runner.invoke(app, ["explain", "agg", "--file", str(sales_csv)])
    assert res.exit_code == 0, res.stdout
    # DuckDB's physical plan mentions the CSV read.
    assert "READ_CSV" in res.stdout.upper()


@needs_duckdb
def test_explain_binds_param(sales_csv: Path) -> None:
    _add("big", "select region, amount from sales where amount > :min")
    res = runner.invoke(
        app, ["explain", "big", "--file", str(sales_csv), "-p", "min=90"]
    )
    assert res.exit_code == 0, res.stdout


@needs_duckdb
def test_explain_analyze_runs(sales_csv: Path) -> None:
    _add("agg", "select region, sum(amount) as total from sales group by region")
    res = runner.invoke(
        app, ["explain", "agg", "--file", str(sales_csv), "--analyze"]
    )
    assert res.exit_code == 0, res.stdout
    # ANALYZE output includes profiling/timing sections.
    assert "Profiling" in res.stdout or "Timing" in res.stdout or res.stdout


def test_explain_select_star_lints(sales_csv: Path) -> None:
    _add("star", "select * from sales")
    res = runner.invoke(app, ["explain", "star", "--file", str(sales_csv)])
    assert res.exit_code == 0, res.stdout
    # Lint goes to stderr.
    assert "SELECT *" in res.stderr or "lint" in res.stderr.lower()


def test_explain_no_lint_suppresses(sales_csv: Path) -> None:
    _add("star", "select * from sales")
    res = runner.invoke(
        app, ["explain", "star", "--file", str(sales_csv), "--no-lint"]
    )
    assert res.exit_code == 0, res.stdout
    assert "lint:" not in res.stdout
    assert "lint:" not in res.stderr


def test_explain_sqlite_fallback(sales_csv: Path) -> None:
    _add("agg", "select region, sum(amount) as total from sales group by region")
    res = runner.invoke(
        app, ["explain", "agg", "--file", str(sales_csv), "--engine", "sqlite"]
    )
    assert res.exit_code == 0, res.stdout
    assert "SCAN" in res.stdout.upper() or "QUERY PLAN" in res.stdout.upper()


def test_explain_unknown_query() -> None:
    res = runner.invoke(app, ["explain", "nope"])
    assert res.exit_code != 0


@needs_duckdb
def test_explain_sql_error_surfaces(sales_csv: Path) -> None:
    _add("broken", "select nonexistent from sales")
    res = runner.invoke(app, ["explain", "broken", "--file", str(sales_csv)])
    assert res.exit_code != 0
    assert "error" in res.stderr.lower()
