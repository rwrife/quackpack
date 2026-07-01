"""Integration tests for param presets (backlog #8).

Covers the ``preset`` command group (``add`` / ``ls`` / ``rm``) and how
``run --preset`` seeds param values:

* ``preset add`` stores a named ``{param: value}`` set on a query (typed like
  ``--param``), warns on unknown params, and refuses to clobber without
  ``--overwrite``;
* ``preset ls`` / ``show`` surface the saved presets;
* ``run --preset NAME`` binds the stored values, and an explicit ``--param``
  passed alongside overrides the matching preset value;
* ``preset rm`` removes one (with a ``--yes`` bypass), and unknown
  query/preset names fail cleanly.

All tests drive the real Typer app against a throwaway ``QUACKPACK_HOME``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from quackpack import cli, engine
from quackpack.cli import app
from quackpack.store import Catalog

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
    # Named ``sales.csv`` so the auto-derived relation is ``sales`` (matches the
    # SQL below without needing read_csv_auto()).
    p = tmp_path / "sales.csv"
    p.write_text(
        "region,amount\nwest,100\neast,250\nwest,75\n",
        encoding="utf-8",
    )
    return p


def _add(name: str, sql: str) -> None:
    res = runner.invoke(app, ["add", "-n", name, "-q", sql])
    assert res.exit_code == 0, res.stdout + res.stderr


SALES_SQL = "select region, sum(amount) as tot from sales where region = :region group by region"
PARAMD_SQL = "select * from t where region = :region and n > :n"


# --------------------------------------------------------------------------
# preset add
# --------------------------------------------------------------------------


def test_preset_add_stores_binding() -> None:
    _add("q", PARAMD_SQL)
    res = runner.invoke(app, ["preset", "add", "q", "west3", "-p", "region=west", "-p", "n=3"])
    assert res.exit_code == 0, res.stdout + res.stderr
    q = Catalog.load().get("q")
    # Values are typed like --param: n becomes an int.
    assert q.presets == {"west3": {"region": "west", "n": 3}}


def test_preset_add_needs_at_least_one_param() -> None:
    _add("q", PARAMD_SQL)
    res = runner.invoke(app, ["preset", "add", "q", "empty"])
    assert res.exit_code == 1
    assert "at least one --param" in res.stderr


def test_preset_add_warns_on_unknown_param() -> None:
    _add("q", PARAMD_SQL)
    res = runner.invoke(app, ["preset", "add", "q", "typo", "-p", "reggion=west"])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "warning" in res.stderr.lower()
    assert "reggion" in res.stderr
    # Still stored despite the warning.
    assert Catalog.load().get("q").presets == {"typo": {"reggion": "west"}}


def test_preset_add_duplicate_needs_overwrite() -> None:
    _add("q", PARAMD_SQL)
    runner.invoke(app, ["preset", "add", "q", "p", "-p", "region=west"])
    dup = runner.invoke(app, ["preset", "add", "q", "p", "-p", "region=east"])
    assert dup.exit_code == 1
    assert "already has a preset" in dup.stderr
    # Unchanged.
    assert Catalog.load().get("q").presets["p"] == {"region": "west"}
    # --overwrite replaces it.
    ok = runner.invoke(app, ["preset", "add", "q", "p", "-p", "region=east", "--overwrite"])
    assert ok.exit_code == 0, ok.stdout + ok.stderr
    assert Catalog.load().get("q").presets["p"] == {"region": "east"}


def test_preset_add_unknown_query_fails() -> None:
    res = runner.invoke(app, ["preset", "add", "ghost", "p", "-p", "n=1"])
    assert res.exit_code == 1
    assert "No query named" in res.stderr


# --------------------------------------------------------------------------
# preset ls / show
# --------------------------------------------------------------------------


def test_preset_ls_lists_presets() -> None:
    _add("q", PARAMD_SQL)
    runner.invoke(app, ["preset", "add", "q", "a", "-p", "region=west", "-p", "n=1"])
    runner.invoke(app, ["preset", "add", "q", "b", "-p", "region=east", "-p", "n=2"])
    res = runner.invoke(app, ["preset", "ls", "q"])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "a" in res.stdout and "b" in res.stdout
    assert "region=west" in res.stdout
    assert "2 presets" in res.stdout


def test_preset_ls_empty_hints_how_to_add() -> None:
    _add("q", PARAMD_SQL)
    res = runner.invoke(app, ["preset", "ls", "q"])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "No presets" in res.stdout


def test_show_includes_presets() -> None:
    _add("q", PARAMD_SQL)
    runner.invoke(app, ["preset", "add", "q", "west1", "-p", "region=west", "-p", "n=1"])
    res = runner.invoke(app, ["show", "q"])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "presets" in res.stdout
    assert "west1" in res.stdout


# --------------------------------------------------------------------------
# run --preset
# --------------------------------------------------------------------------


@needs_duckdb
def test_run_applies_preset(sales_csv: Path) -> None:
    _add("sales", SALES_SQL)
    runner.invoke(app, ["preset", "add", "sales", "west", "-p", "region=west"])
    res = runner.invoke(
        app, ["run", "sales", "--preset", "west", "-f", str(sales_csv), "-F", "csv"]
    )
    assert res.exit_code == 0, res.stdout + res.stderr
    # west rows: 100 + 75 = 175
    assert "west,175" in res.stdout


@needs_duckdb
def test_run_param_overrides_preset(sales_csv: Path) -> None:
    _add("sales", SALES_SQL)
    runner.invoke(app, ["preset", "add", "sales", "west", "-p", "region=west"])
    res = runner.invoke(
        app,
        ["run", "sales", "--preset", "west", "-p", "region=east", "-f", str(sales_csv), "-F", "csv"],
    )
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "east,250" in res.stdout
    assert "west" not in res.stdout.split("\n", 1)[-1]  # header may say region


def test_run_unknown_preset_fails(sales_csv: Path) -> None:
    _add("sales", SALES_SQL)
    res = runner.invoke(
        app, ["run", "sales", "--preset", "nope", "-f", str(sales_csv), "-F", "csv"]
    )
    assert res.exit_code == 1
    assert "no preset named" in res.stderr


# --------------------------------------------------------------------------
# preset rm
# --------------------------------------------------------------------------


def test_preset_rm_with_yes_removes() -> None:
    _add("q", PARAMD_SQL)
    runner.invoke(app, ["preset", "add", "q", "p", "-p", "region=west"])
    res = runner.invoke(app, ["preset", "rm", "q", "p", "-y"])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert Catalog.load().get("q").presets == {}


def test_preset_rm_missing_preset_fails() -> None:
    _add("q", PARAMD_SQL)
    res = runner.invoke(app, ["preset", "rm", "q", "ghost", "-y"])
    assert res.exit_code == 1
    assert "no preset named" in res.stderr


def test_preset_rm_declined_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _add("q", PARAMD_SQL)
    runner.invoke(app, ["preset", "add", "q", "p", "-p", "region=west"])
    # Answer "n" to the confirm prompt.
    res = runner.invoke(app, ["preset", "rm", "q", "p"], input="n\n")
    assert res.exit_code == 0
    assert "Aborted" in res.stdout
    assert "p" in Catalog.load().get("q").presets
