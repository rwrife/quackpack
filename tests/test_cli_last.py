"""Integration tests for ``quackpack last`` (backlog #8).

``last <name>`` re-shows the result cached by a previous ``run`` — straight from
the snapshot sidecar, without spinning up the engine or touching the data file.
These tests drive the real Typer app against a throwaway ``QUACKPACK_HOME`` and
cover:

* the happy path in ``table`` and ``json`` (matches what ``snapshot show`` holds);
* a provenance line noting capture age (and params, when recorded);
* the missing-snapshot error path (``error:`` on stderr, exit 1);
* that ``last`` never invokes the query engine (``run_query`` is patched to blow
  up, yet ``last`` succeeds because it only reads the cache);
* target/param flags being rejected as a usage error.
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
# happy paths
# --------------------------------------------------------------------------


@needs_duckdb
def test_last_table_matches_cached_result(tmp_path: Path) -> None:
    csv = _write_sales(tmp_path, "id,region,amount\n1,west,100\n2,east,250\n")
    _add()
    _run(csv, "--key", "id")

    res = runner.invoke(app, ["last", "sales"])
    assert res.exit_code == 0, res.stderr
    out = res.stdout
    # The cached rows are rendered...
    assert "west" in out and "east" in out and "250" in out
    # ...under a provenance/header line noting it's cached.
    assert "cached" in out
    assert "2 rows" in out


@needs_duckdb
def test_last_json_envelope_and_stderr_provenance(tmp_path: Path) -> None:
    csv = _write_sales(tmp_path, "id,region,amount\n1,west,100\n2,east,250\n")
    _add()
    _run(csv, "--key", "id")

    res = runner.invoke(app, ["last", "sales", "--format", "json"])
    assert res.exit_code == 0, res.stderr
    # stdout is clean JSON (provenance went to stderr) so it pipes cleanly.
    payload = json.loads(res.stdout)
    assert payload == [
        {"id": 1, "region": "west", "amount": 100},
        {"id": 2, "region": "east", "amount": 250},
    ]
    assert "cached" in res.stderr


@needs_duckdb
def test_last_matches_snapshot_show(tmp_path: Path) -> None:
    csv = _write_sales(tmp_path, "id,region,amount\n1,west,100\n2,east,250\n")
    _add()
    _run(csv, "--key", "id")

    last_json = runner.invoke(app, ["last", "sales", "--format", "json"])
    show_json = runner.invoke(app, ["snapshot", "show", "sales", "--format", "json"])
    assert last_json.exit_code == 0
    assert show_json.exit_code == 0
    assert json.loads(last_json.stdout) == json.loads(show_json.stdout)


@needs_duckdb
def test_last_reports_params_provenance(tmp_path: Path) -> None:
    csv = _write_sales(tmp_path, "id,region,amount\n1,west,100\n2,east,250\n")
    _add(sql="select id, region, amount from sales where amount >= :floor order by id")
    _run(csv, "-p", "floor=200")

    res = runner.invoke(app, ["last", "sales"])
    assert res.exit_code == 0, res.stderr
    assert "floor" in res.stdout  # recorded param surfaced in provenance


# --------------------------------------------------------------------------
# does not invoke the engine
# --------------------------------------------------------------------------


@needs_duckdb
def test_last_never_runs_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    csv = _write_sales(tmp_path, "id,region,amount\n1,west,100\n2,east,250\n")
    _add()
    _run(csv, "--key", "id")

    # If `last` tried to execute the query, this would blow up. It must not.
    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("last must not invoke the query engine")

    monkeypatch.setattr("quackpack.cli.run_query", _boom)

    # A broken/absent data file is likewise irrelevant — cache only.
    res = runner.invoke(app, ["last", "sales"])
    assert res.exit_code == 0, res.stderr
    assert "east" in res.stdout


# --------------------------------------------------------------------------
# error / usage paths
# --------------------------------------------------------------------------


@needs_duckdb
def test_last_missing_snapshot_errors(tmp_path: Path) -> None:
    _add()  # query exists, but was never run -> no cache
    res = runner.invoke(app, ["last", "sales"])
    assert res.exit_code == 1
    assert "no cached result" in res.stderr.lower()
    assert "run it first" in res.stderr.lower()


@needs_duckdb
def test_last_no_snapshot_history_reports_no_cache(tmp_path: Path) -> None:
    csv = _write_sales(tmp_path, "id,region,amount\n1,west,100\n")
    _add()
    _run(csv, "--no-snapshot")  # opted out of caching
    res = runner.invoke(app, ["last", "sales"])
    assert res.exit_code == 1
    assert "no cached result" in res.stderr.lower()


def test_last_unknown_query_errors(tmp_path: Path) -> None:
    res = runner.invoke(app, ["last", "nope"])
    assert res.exit_code == 1
    assert "error:" in res.stderr.lower()


def test_last_rejects_target_flags_as_usage_error(tmp_path: Path) -> None:
    # `--file`/`--param` are irrelevant to a cache read and are not defined on
    # `last`; Typer rejects the unknown option as a usage error (exit code 2).
    res = runner.invoke(app, ["last", "sales", "--file", "whatever.csv"])
    assert res.exit_code == 2
