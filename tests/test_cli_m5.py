"""Integration tests for M5: search, edit, and run history.

Builds on the M2/M3/M4 CLI plumbing and focuses on what M5 adds:

* ``quackpack search <text>`` finds queries by name / SQL / desc / tag and
  reports a match count (or a friendly "no matches");
* ``quackpack edit <name>`` opens ``$EDITOR`` (here monkeypatched), saves the
  edited SQL, and **re-parses** ``:params`` on save; unchanged/empty edits are
  safe no-ops;
* every ``run`` bumps per-query history, so ``ls`` shows "last run …" and
  ``show`` reports run count + last outcome (including ``(error)`` on failure).

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
    p = tmp_path / "sales.csv"
    p.write_text(
        "region,amount\nwest,100\neast,250\nwest,75\n", encoding="utf-8"
    )
    return p


def _add(name: str, sql: str, *, tags: str | None = None, desc: str | None = None) -> None:
    args = ["add", "-n", name, "-q", sql]
    if tags:
        args += ["-t", tags]
    if desc:
        args += ["-d", desc]
    res = runner.invoke(app, args)
    assert res.exit_code == 0, res.stdout + res.stderr


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------


def test_search_matches_across_fields() -> None:
    _add("orders", "select * from orders", tags="sales", desc="all orders")
    _add("people", "select * from users", tags="auth", desc="everyone")

    # By name/desc substring.
    res = runner.invoke(app, ["search", "order"])
    assert res.exit_code == 0, res.stderr
    assert "orders" in res.stdout
    assert "people" not in res.stdout
    assert "1 match" in res.stdout

    # By tag.
    res = runner.invoke(app, ["search", "auth"])
    assert res.exit_code == 0
    assert "people" in res.stdout and "orders" not in res.stdout

    # By SQL body — both contain "select".
    res = runner.invoke(app, ["search", "select"])
    assert res.exit_code == 0
    assert "orders" in res.stdout and "people" in res.stdout
    assert "2 matches" in res.stdout


def test_search_no_matches_is_friendly() -> None:
    _add("orders", "select 1")
    res = runner.invoke(app, ["search", "zzzznope"])
    assert res.exit_code == 0
    assert "No queries matching" in res.stdout


def test_search_is_case_insensitive() -> None:
    _add("Orders", "SELECT 1", desc="Big Report")
    res = runner.invoke(app, ["search", "big report"])
    assert res.exit_code == 0
    assert "Orders" in res.stdout


# --------------------------------------------------------------------------
# edit
# --------------------------------------------------------------------------


def _patch_editor(monkeypatch: pytest.MonkeyPatch, new_text):
    """Make ``cli._edit_text`` return *new_text* (or None) instead of spawning $EDITOR."""
    captured: dict[str, object] = {}

    def fake_edit(initial, *args, **kwargs):
        captured["seen"] = initial
        return new_text

    monkeypatch.setattr(cli, "_edit_text", fake_edit)
    return captured


def test_edit_saves_new_sql_and_reparses_params(monkeypatch: pytest.MonkeyPatch) -> None:
    _add("q", "select * from t where id = :id")
    captured = _patch_editor(
        monkeypatch, "select * from t where id = :id and ts > :since"
    )

    res = runner.invoke(app, ["edit", "q"])
    assert res.exit_code == 0, res.stderr
    # The editor was handed the *current* SQL to edit.
    assert captured["seen"] == "select * from t where id = :id"
    assert "updated" in res.stdout

    stored = Catalog.load().get("q")
    assert stored.sql == "select * from t where id = :id and ts > :since"
    # New param picked up automatically; order preserved.
    assert stored.params == ["id", "since"]


def test_edit_no_change_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    _add("q", "select 1")
    # Editor returns identical text (after strip).
    _patch_editor(monkeypatch, "select 1\n")

    res = runner.invoke(app, ["edit", "q"])
    assert res.exit_code == 0
    assert "no changes" in res.stdout.lower()


def test_edit_aborted_editor_returns_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    _add("q", "select 1")
    # typer.edit returns None when the editor exits without saving.
    _patch_editor(monkeypatch, None)

    res = runner.invoke(app, ["edit", "q"])
    assert res.exit_code == 0
    assert "no changes" in res.stdout.lower()
    assert Catalog.load().get("q").sql == "select 1"


def test_edit_empty_result_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _add("q", "select 1")
    _patch_editor(monkeypatch, "   \n  ")

    res = runner.invoke(app, ["edit", "q"])
    assert res.exit_code == 1
    assert "empty" in res.stderr.lower()
    # Original untouched.
    assert Catalog.load().get("q").sql == "select 1"


def test_edit_missing_query_errors() -> None:
    res = runner.invoke(app, ["edit", "ghost"])
    assert res.exit_code == 1
    assert "No query named" in res.stderr


def test_edit_dropping_param_reparses(monkeypatch: pytest.MonkeyPatch) -> None:
    _add("q", "select * from t where id = :id")
    _patch_editor(monkeypatch, "select * from t")  # no params now

    res = runner.invoke(app, ["edit", "q"])
    assert res.exit_code == 0
    stored = Catalog.load().get("q")
    assert stored.params == []


def test_resolve_editor_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    # Explicit --editor beats everything.
    monkeypatch.setenv("VISUAL", "visual-ed")
    monkeypatch.setenv("EDITOR", "editor-ed")
    assert cli._resolve_editor("explicit-ed") == "explicit-ed"
    # Then $VISUAL.
    assert cli._resolve_editor(None) == "visual-ed"
    # Then $EDITOR.
    monkeypatch.delenv("VISUAL")
    assert cli._resolve_editor(None) == "editor-ed"
    # Then a built-in default.
    monkeypatch.delenv("EDITOR")
    assert cli._resolve_editor(None) in {"vi", "notepad"}


# --------------------------------------------------------------------------
# run history → ls / show
# --------------------------------------------------------------------------


def test_ls_shows_never_then_recency(sales_csv: Path) -> None:
    _add("sumamt", "select sum(amount) as t from sales", tags="demo")

    # Before any run: "never".
    res = runner.invoke(app, ["ls"])
    assert res.exit_code == 0
    assert "last run" in res.stdout  # column header present
    assert "never" in res.stdout

    # Run it (SQLite engine so the test works without duckdb installed)...
    run = runner.invoke(app, ["run", "sumamt", "--file", str(sales_csv), "-e", "sqlite"])
    assert run.exit_code == 0, run.stderr

    # ...now ls reflects a recent run, no longer "never".
    res = runner.invoke(app, ["ls"])
    assert res.exit_code == 0
    assert "never" not in res.stdout
    assert "just now" in res.stdout


def test_run_bumps_history_count_and_status(sales_csv: Path) -> None:
    _add("sumamt", "select sum(amount) as t from sales")
    for _ in range(3):
        res = runner.invoke(
            app, ["run", "sumamt", "--file", str(sales_csv), "-e", "sqlite"]
        )
        assert res.exit_code == 0, res.stderr

    q = Catalog.load().get("sumamt")
    assert q.run_count == 3
    assert q.last_status == "ok"
    assert q.last_run  # timestamp recorded


def test_show_reports_run_history(sales_csv: Path) -> None:
    _add("sumamt", "select sum(amount) as t from sales")
    runner.invoke(app, ["run", "sumamt", "--file", str(sales_csv), "-e", "sqlite"])

    res = runner.invoke(app, ["show", "sumamt"])
    assert res.exit_code == 0
    assert "runs: 1" in res.stdout
    assert "last run" in res.stdout


def test_failed_run_records_error_status(sales_csv: Path) -> None:
    # A query against a missing relation fails; history must mark it (error)
    # and the count still increments.
    _add("broken", "select * from no_such_relation")
    res = runner.invoke(app, ["run", "broken", "--file", str(sales_csv), "-e", "sqlite"])
    assert res.exit_code == 1

    q = Catalog.load().get("broken")
    assert q.run_count == 1
    assert q.last_status == "error"

    ls = runner.invoke(app, ["ls"])
    assert "(error)" in ls.stdout
