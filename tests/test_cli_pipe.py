"""Integration tests for ``quackpack pipe`` (issue #7 — stash-on-the-fly).

``pipe`` runs an ad-hoc query straight from stdin (or ``-q``/``--sql-file``) and
then offers to stash it. These tests drive the real Typer app against a
throwaway ``QUACKPACK_HOME`` and cover:

* the fast path — SQL on stdin runs and renders like ``run`` (table/json, params);
* saving: ``--save-as`` stashes non-interactively and the query lands in the
  catalog; an interactive prompt stashes (or, blank, skips);
* the nudge — a query piped before is flagged on the next pipe;
* guard rails — ``--no-save`` never prompts, a duplicate name is a clean error,
  and a bad ``--format`` is rejected before anything runs.

The SQLite engine is always available, so the default-path tests use it via
``-e sqlite`` to stay runnable even where DuckDB isn't installed; a couple of
DuckDB-specific renders are guarded behind ``needs_duckdb``.
"""

from __future__ import annotations

import json
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
    p = tmp_path / "sales.csv"
    p.write_text(
        "region,amount\nwest,100\neast,250\nwest,75\n",
        encoding="utf-8",
    )
    return p


# --------------------------------------------------------------------------
# Running from stdin
# --------------------------------------------------------------------------


def test_pipe_runs_sql_from_stdin(sales_csv: Path) -> None:
    sql = "select region, sum(amount) as total from sales group by region order by region"
    res = runner.invoke(
        app,
        ["pipe", "--file", str(sales_csv), "-e", "sqlite", "-F", "csv", "--no-save"],
        input=sql,
    )
    assert res.exit_code == 0, res.stdout + res.stderr
    body = [ln for ln in res.stdout.strip().splitlines() if ln]
    assert body == ["region,total", "east,250", "west,175"]


def test_pipe_binds_param(sales_csv: Path) -> None:
    sql = "select amount from sales where amount >= :min order by amount"
    res = runner.invoke(
        app,
        [
            "pipe",
            "--file",
            str(sales_csv),
            "-e",
            "sqlite",
            "-p",
            "min=100",
            "-F",
            "json",
            "--no-save",
        ],
        input=sql,
    )
    assert res.exit_code == 0, res.stdout + res.stderr
    assert json.loads(res.stdout) == [{"amount": 100}, {"amount": 250}]


def test_pipe_accepts_inline_query(sales_csv: Path) -> None:
    res = runner.invoke(
        app,
        [
            "pipe",
            "-q",
            "select count(*) as n from sales",
            "--file",
            str(sales_csv),
            "-e",
            "sqlite",
            "-F",
            "json",
            "--no-save",
        ],
    )
    assert res.exit_code == 0, res.stdout + res.stderr
    assert json.loads(res.stdout) == [{"n": 3}]


def test_pipe_reads_sql_file(sales_csv: Path, tmp_path: Path) -> None:
    qfile = tmp_path / "q.sql"
    qfile.write_text("select count(*) as n from sales\n", encoding="utf-8")
    res = runner.invoke(
        app,
        [
            "pipe",
            "--sql-file",
            str(qfile),
            "--file",
            str(sales_csv),
            "-e",
            "sqlite",
            "-F",
            "json",
            "--no-save",
        ],
    )
    assert res.exit_code == 0, res.stdout + res.stderr
    assert json.loads(res.stdout) == [{"n": 3}]


@needs_duckdb
def test_pipe_runs_with_duckdb_default(sales_csv: Path) -> None:
    sql = "select sum(amount) as total from sales"
    res = runner.invoke(
        app,
        ["pipe", "--file", str(sales_csv), "-F", "json", "--no-save"],
        input=sql,
    )
    assert res.exit_code == 0, res.stdout + res.stderr
    assert json.loads(res.stdout) == [{"total": 425}]


# --------------------------------------------------------------------------
# Stashing
# --------------------------------------------------------------------------


def test_save_as_stashes_into_catalog(sales_csv: Path, tmp_path: Path) -> None:
    sql = "select count(*) as n from sales"
    res = runner.invoke(
        app,
        [
            "pipe",
            "--file",
            str(sales_csv),
            "-e",
            "sqlite",
            "--save-as",
            "rowcount",
            "-t",
            "adhoc,sales",
            "-d",
            "how many rows",
        ],
        input=sql,
    )
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "stashed" in res.stdout and "rowcount" in res.stdout

    cat = Catalog.load()
    saved = cat.get("rowcount")
    assert saved.sql == sql
    assert saved.tags == ["adhoc", "sales"]
    assert saved.desc == "how many rows"


def test_save_as_records_detected_params(sales_csv: Path) -> None:
    sql = "select amount from sales where amount >= :min order by amount"
    res = runner.invoke(
        app,
        [
            "pipe",
            "--file",
            str(sales_csv),
            "-e",
            "sqlite",
            "-p",
            "min=100",
            "--save-as",
            "floor",
        ],
        input=sql,
    )
    assert res.exit_code == 0, res.stdout + res.stderr
    saved = Catalog.load().get("floor")
    assert saved.params == ["min"]


def test_interactive_prompt_stashes(
    sales_csv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: "kept")

    res = runner.invoke(
        app,
        ["pipe", "-q", "select 1 as x", "--file", str(sales_csv), "-e", "sqlite"],
    )
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "stashed" in res.stdout
    assert Catalog.load().get("kept").sql == "select 1 as x"


def test_interactive_blank_name_skips_stash(
    sales_csv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: "")  # blank → skip

    res = runner.invoke(
        app,
        ["pipe", "-q", "select 1 as x", "--file", str(sales_csv), "-e", "sqlite"],
    )
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "not stashed" in res.stdout
    assert Catalog.load().names() == []


def test_duplicate_save_name_is_clean_error(
    sales_csv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pre-seed a query, then try to stash under the same name.
    runner.invoke(app, ["add", "-n", "dup", "-q", "select 1"])
    res = runner.invoke(
        app,
        ["pipe", "-q", "select 2 as y", "--file", str(sales_csv), "-e", "sqlite", "--save-as", "dup"],
    )
    assert res.exit_code == 1
    assert "already exists" in res.stderr


def test_empty_save_as_is_rejected(sales_csv: Path) -> None:
    res = runner.invoke(
        app,
        ["pipe", "-q", "select 1", "--file", str(sales_csv), "-e", "sqlite", "--save-as", "   "],
    )
    assert res.exit_code == 1
    assert "non-empty name" in res.stderr


# --------------------------------------------------------------------------
# The nudge
# --------------------------------------------------------------------------


def test_repeat_pipe_nudges_to_stash(sales_csv: Path) -> None:
    sql = "select count(*) as n from sales"
    args = ["pipe", "--file", str(sales_csv), "-e", "sqlite", "--no-save"]
    first = runner.invoke(app, args, input=sql)
    assert first.exit_code == 0, first.stdout + first.stderr
    # First pipe: no nudge yet.
    assert "piped this" not in first.stdout

    second = runner.invoke(app, args, input=sql)
    assert second.exit_code == 0, second.stdout + second.stderr
    # Second pipe of the *same* query: nudge appears with the 2× count.
    assert "piped this 2" in second.stdout


def test_interactive_repeat_shows_stronger_nudge(
    sales_csv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sql = "select 7 as lucky"
    # Prime the pipe log with one prior run (non-interactive, no save).
    runner.invoke(
        app,
        ["pipe", "--file", str(sales_csv), "-e", "sqlite", "--no-save"],
        input=sql,
    )

    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: "")  # skip the save

    res = runner.invoke(
        app,
        ["pipe", "-q", sql, "--file", str(sales_csv), "-e", "sqlite"],
    )
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "piped this 2" in res.stdout


# --------------------------------------------------------------------------
# Guard rails
# --------------------------------------------------------------------------


def test_no_save_never_prompts(sales_csv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Even pretending to be interactive, --no-save must not call prompt().
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)

    def boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("prompt() must not be called with --no-save")

    monkeypatch.setattr(cli.typer, "prompt", boom)

    res = runner.invoke(
        app,
        ["pipe", "-q", "select 1", "--file", str(sales_csv), "-e", "sqlite", "--no-save"],
    )
    assert res.exit_code == 0, res.stdout + res.stderr
    assert Catalog.load().names() == []


def test_bad_format_is_rejected(sales_csv: Path) -> None:
    res = runner.invoke(
        app,
        ["pipe", "-q", "select 1", "--file", str(sales_csv), "-F", "yaml", "--no-save"],
    )
    assert res.exit_code == 1
    assert "Unknown --format" in res.stderr


def test_engine_error_surfaces_and_does_not_stash(
    sales_csv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A broken query should fail cleanly; even with --save-as we must not stash
    # something that never ran.
    res = runner.invoke(
        app,
        [
            "pipe",
            "-q",
            "select * from no_such_table",
            "--file",
            str(sales_csv),
            "-e",
            "sqlite",
            "--save-as",
            "broken",
        ],
    )
    assert res.exit_code == 1
    assert "SQL error" in res.stderr
    assert Catalog.load().names() == []
