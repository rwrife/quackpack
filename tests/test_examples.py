"""End-to-end tests for the bundled M6 examples.

These guard the M6 "definition of done": a stranger can stash + run the starter
queries from the README in a couple of minutes. We drive the real Typer CLI
against the shipped ``examples/`` dataset and assert each starter query produces
the documented result, and that the curated ``examples/pack.yaml`` stays in sync
with the standalone ``.sql`` files.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from quackpack import engine
from quackpack.cli import app

runner = CliRunner()

needs_duckdb = pytest.mark.skipif(
    not engine.DUCKDB_AVAILABLE, reason="duckdb not installed"
)

# Repo root = two levels up from this test file (tests/ -> repo).
REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples"
SALES_CSV = EXAMPLES / "sales.csv"

STARTERS = {
    "top-regions": "top-regions.sql",
    "big-orders": "big-orders.sql",
    "product-mix": "product-mix.sql",
}


def test_examples_dir_is_shipped() -> None:
    """The example assets the README points at must actually exist."""
    assert SALES_CSV.is_file(), "examples/sales.csv is missing"
    assert (EXAMPLES / "pack.yaml").is_file(), "examples/pack.yaml is missing"
    assert (EXAMPLES / "README.md").is_file(), "examples/README.md is missing"
    for fname in STARTERS.values():
        assert (EXAMPLES / fname).is_file(), f"examples/{fname} is missing"


def test_bundled_pack_sql_matches_sql_files() -> None:
    """The one-shot pack.yaml must mirror the standalone .sql files verbatim."""
    pack = yaml.safe_load((EXAMPLES / "pack.yaml").read_text(encoding="utf-8"))
    by_name = {q["name"]: q for q in pack["queries"]}
    assert set(by_name) == set(STARTERS), "pack.yaml names drifted from STARTERS"

    for name, fname in STARTERS.items():
        disk_sql = (EXAMPLES / fname).read_text(encoding="utf-8").rstrip("\n")
        assert by_name[name]["sql"] == disk_sql, (
            f"examples/pack.yaml SQL for {name!r} is out of sync with {fname}"
        )

    # big-orders is the parameterized one; its :min must be recorded.
    assert by_name["big-orders"]["params"] == ["min"]


@needs_duckdb
def test_quickstart_add_then_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The README's `add -f` + `run --file` flow yields the documented rows."""
    monkeypatch.setenv("QUACKPACK_HOME", str(tmp_path))

    for name, fname in STARTERS.items():
        res = runner.invoke(app, ["add", "-n", name, "-f", str(EXAMPLES / fname)])
        assert res.exit_code == 0, res.stdout

    # top-regions: east leads at 995 across 4 regions.
    res = runner.invoke(app, ["run", "top-regions", "--file", str(SALES_CSV)])
    assert res.exit_code == 0, res.stdout
    assert "east" in res.stdout and "995" in res.stdout
    assert "4 rows" in res.stdout

    # big-orders with a bound :param keeps only the >= 300 orders.
    res = runner.invoke(
        app, ["run", "big-orders", "--file", str(SALES_CSV), "--param", "min=300"]
    )
    assert res.exit_code == 0, res.stdout
    assert "600" in res.stdout  # the top order survives the floor
    # The smallest order (2026-01-11, amount 50) is below the floor and excluded.
    assert "2026-01-11" not in res.stdout
    assert "4 rows" in res.stdout

    # product-mix: revenue shares should be present and gadget should lead.
    res = runner.invoke(app, ["run", "product-mix", "--file", str(SALES_CSV)])
    assert res.exit_code == 0, res.stdout
    assert "gadget" in res.stdout
    assert "44.4" in res.stdout  # gadget's pct_of_revenue
    assert "3 rows" in res.stdout


@needs_duckdb
def test_bundled_pack_loads_and_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one-shot ``pack.yaml`` load lists and runs the starters.

    The bundled pack is copied into a throwaway home first so that ``run``
    recording history never mutates the tracked ``examples/pack.yaml``.
    """
    shutil.copy(EXAMPLES / "pack.yaml", tmp_path / "pack.yaml")
    monkeypatch.setenv("QUACKPACK_HOME", str(tmp_path))

    res = runner.invoke(app, ["ls"])
    assert res.exit_code == 0, res.stdout
    for name in STARTERS:
        assert name in res.stdout

    res = runner.invoke(
        app, ["run", "big-orders", "--file", str(SALES_CSV), "--param", "min=400"]
    )
    assert res.exit_code == 0, res.stdout
    # Only two orders clear the 400 floor (600 and 450).
    assert "2 rows" in res.stdout
