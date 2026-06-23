"""Integration tests for the M2 CLI commands: add / ls / show / rm.

Uses Typer's ``CliRunner`` against a temp ``QUACKPACK_HOME`` so the commands hit
a throwaway catalog on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from quackpack.cli import app
from quackpack.store import Catalog

runner = CliRunner()


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("QUACKPACK_HOME", str(tmp_path))
    return tmp_path


def test_add_inline_then_persisted() -> None:
    result = runner.invoke(
        app,
        ["add", "-n", "top", "-q", "select * from t where id = :id", "-t", "sales,adhoc", "-d", "tops"],
    )
    assert result.exit_code == 0, result.stdout
    assert "saved" in result.stdout
    assert "top" in result.stdout
    assert "id" in result.stdout  # param echoed

    stored = Catalog.load().get("top")
    assert stored.sql == "select * from t where id = :id"
    assert stored.tags == ["sales", "adhoc"]
    assert stored.params == ["id"]


def test_add_from_file(tmp_path: Path) -> None:
    sqlfile = tmp_path / "q.sql"
    sqlfile.write_text("select 42", encoding="utf-8")
    result = runner.invoke(app, ["add", "-n", "ans", "-f", str(sqlfile)])
    assert result.exit_code == 0, result.stdout
    assert Catalog.load().get("ans").sql == "select 42"


def test_add_from_stdin() -> None:
    result = runner.invoke(app, ["add", "-n", "piped"], input="select 7\n")
    assert result.exit_code == 0, result.stdout
    assert Catalog.load().get("piped").sql == "select 7"


def test_add_duplicate_fails_without_overwrite() -> None:
    assert runner.invoke(app, ["add", "-n", "dup", "-q", "select 1"]).exit_code == 0
    dup = runner.invoke(app, ["add", "-n", "dup", "-q", "select 2"])
    assert dup.exit_code == 1
    assert "already exists" in dup.stderr
    # Unchanged on disk.
    assert Catalog.load().get("dup").sql == "select 1"


def test_add_overwrite_replaces() -> None:
    runner.invoke(app, ["add", "-n", "dup", "-q", "select 1"])
    ok = runner.invoke(app, ["add", "-n", "dup", "-q", "select 2", "--overwrite"])
    assert ok.exit_code == 0, ok.stdout
    assert Catalog.load().get("dup").sql == "select 2"


def test_add_empty_query_fails() -> None:
    result = runner.invoke(app, ["add", "-n", "empty", "-q", "   "])
    assert result.exit_code == 1


def test_ls_empty() -> None:
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    assert "No queries" in result.stdout


def test_ls_lists_and_filters_by_tag() -> None:
    runner.invoke(app, ["add", "-n", "alpha", "-q", "select 1", "-t", "x,y"])
    runner.invoke(app, ["add", "-n", "zeta", "-q", "select 2", "-t", "x"])

    out = runner.invoke(app, ["ls"]).stdout
    assert "alpha" in out and "zeta" in out

    only_y = runner.invoke(app, ["ls", "--tag", "y"]).stdout
    assert "alpha" in only_y
    assert "zeta" not in only_y


def test_show_displays_sql_and_meta() -> None:
    runner.invoke(
        app, ["add", "-n", "rep", "-q", "select * from t where d = :d", "-d", "report", "-t", "sales"]
    )
    result = runner.invoke(app, ["show", "rep"])
    assert result.exit_code == 0, result.stdout
    text = result.stdout
    assert "rep" in text
    assert "select" in text.lower()
    assert "report" in text
    assert "sales" in text


def test_show_missing_fails() -> None:
    result = runner.invoke(app, ["show", "ghost"])
    assert result.exit_code == 1


def test_rm_with_yes_flag() -> None:
    runner.invoke(app, ["add", "-n", "kill", "-q", "select 1"])
    result = runner.invoke(app, ["rm", "kill", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "removed" in result.stdout
    assert Catalog.load().names() == []


def test_rm_prompt_abort_keeps_query() -> None:
    runner.invoke(app, ["add", "-n", "keep", "-q", "select 1"])
    result = runner.invoke(app, ["rm", "keep"], input="n\n")
    assert "Aborted" in result.stdout
    assert Catalog.load().names() == ["keep"]


def test_rm_missing_fails() -> None:
    result = runner.invoke(app, ["rm", "ghost", "--yes"])
    assert result.exit_code == 1
