"""Integration tests for query templating in the CLI (backlog #10).

Drives the real Typer app against a throwaway ``QUACKPACK_HOME`` to cover:

* ``show`` lists a query's ``references`` and, with ``--expanded``, prints the
  flattened SQL (refs inlined as parenthesised subqueries);
* ``show --expanded`` / ``run`` surface unknown-reference and cycle errors as a
  clean ``error:`` on stderr with exit code 1;
* ``run`` executes the *expanded* SQL and binds params drawn from a referenced
  query (``:file`` from the inner query, ``:min`` on the outer one).

DuckDB-dependent ``run`` assertions skip gracefully where duckdb is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from quackpack import cli, engine
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
    # Named sales.csv so the auto-derived relation is `sales`.
    p = tmp_path / "sales.csv"
    p.write_text(
        "region,amount\nwest,100\neast,250\nwest,75\nsouth,500\n",
        encoding="utf-8",
    )
    return p


def _add(name: str, sql: str) -> None:
    res = runner.invoke(app, ["add", "-n", name, "-q", sql])
    assert res.exit_code == 0, res.stdout + res.stderr


# --------------------------------------------------------------------------
# show
# --------------------------------------------------------------------------


def test_show_lists_references() -> None:
    _add("base", "select * from t")
    _add("wrap", "select * from {{ base }} limit :n")
    res = runner.invoke(app, ["show", "wrap"])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "references:" in res.stdout
    assert "base" in res.stdout


def test_show_without_refs_omits_references_line() -> None:
    _add("plain", "select * from t where id = :id")
    res = runner.invoke(app, ["show", "plain"])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "references:" not in res.stdout


def test_show_expanded_inlines_referenced_query() -> None:
    _add("base", "select * from read_csv_auto(:file) where ok")
    _add("wrap", "select * from {{ base }} limit :n")
    res = runner.invoke(app, ["show", "--expanded", "wrap"])
    assert res.exit_code == 0, res.stdout + res.stderr
    # The rendered SQL should contain the inlined, parenthesised inner query.
    out = res.stdout
    assert "(select * from read_csv_auto(:file) where ok)" in out
    # And the raw brace token should be gone.
    assert "{{" not in out


def test_show_default_keeps_raw_reference() -> None:
    _add("base", "select 1")
    _add("wrap", "select * from {{ base }}")
    res = runner.invoke(app, ["show", "wrap"])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "{{ base }}" in res.stdout


def test_show_expanded_unknown_ref_errors() -> None:
    _add("wrap", "select * from {{ ghost }}")
    res = runner.invoke(app, ["show", "--expanded", "wrap"])
    assert res.exit_code == 1
    assert "ghost" in res.stderr
    assert "does not exist" in res.stderr


def test_show_expanded_cycle_errors() -> None:
    _add("a", "select * from {{ b }}")
    _add("b", "select * from {{ a }}")
    res = runner.invoke(app, ["show", "--expanded", "a"])
    assert res.exit_code == 1
    assert "cycle" in res.stderr.lower()
    assert "a -> b -> a" in res.stderr


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


@needs_duckdb
def test_run_expands_and_binds_nested_params(sales_csv: Path) -> None:
    # Inner query exposes the `sales` relation; outer filters and orders.
    _add("sales_all", "select region, amount from sales")
    _add(
        "big_sales",
        "select * from {{ sales_all }} where amount > :min order by amount desc",
    )
    res = runner.invoke(
        app,
        [
            "run",
            "big_sales",
            "--file",
            str(sales_csv),
            "--param",
            "min=100",
            "-F",
            "csv",
        ],
    )
    assert res.exit_code == 0, res.stdout + res.stderr
    lines = [ln for ln in res.stdout.strip().splitlines() if ln]
    assert lines[0] == "region,amount"
    # Only amounts strictly > 100 survive: 500 and 250 (100 and 75 excluded).
    assert lines[1] == "south,500"
    assert lines[2] == "east,250"
    assert len(lines) == 3


def test_run_unknown_ref_errors_cleanly(sales_csv: Path) -> None:
    _add("dangling", "select * from {{ ghost }}")
    res = runner.invoke(app, ["run", "dangling", "--file", str(sales_csv)])
    assert res.exit_code == 1
    assert "ghost" in res.stderr
    # The catalog run should not have been recorded as an attempt for a
    # composition error (it failed before execution).
    from quackpack.store import Catalog

    assert Catalog.load().get("dangling").run_count == 0


def test_run_cycle_errors_cleanly(sales_csv: Path) -> None:
    _add("x", "select * from {{ y }}")
    _add("y", "select * from {{ x }}")
    res = runner.invoke(app, ["run", "x", "--file", str(sales_csv)])
    assert res.exit_code == 1
    assert "cycle" in res.stderr.lower()
