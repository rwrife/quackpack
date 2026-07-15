"""Tests for glob / multi-file fan-out on ``run`` (issue #32).

Covers the engine-level :func:`quackpack.glob.run_query_multi` and the CLI
``run --file '<glob>'`` surface: default UNION, ``--per-file`` output, the
``--with-source`` provenance column, csv/parquet coverage, the empty-glob
error, and the non-glob misuse guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from quackpack import engine
from quackpack.cli import app
from quackpack.glob import (
    SOURCE_COLUMN,
    expand_glob,
    is_glob,
    run_query_multi,
)
from quackpack.engine import EngineError

runner = CliRunner()

needs_duckdb = pytest.mark.skipif(
    not engine.DUCKDB_AVAILABLE, reason="duckdb not installed"
)


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("QUACKPACK_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def csv_dir(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    d.mkdir()
    (d / "one.csv").write_text("a,b\n1,x\n2,y\n", encoding="utf-8")
    (d / "two.csv").write_text("a,b\n3,z\n", encoding="utf-8")
    return d


@pytest.fixture
def parquet_dir(tmp_path: Path) -> Path:
    duckdb = pytest.importorskip("duckdb")
    d = tmp_path / "pq"
    d.mkdir()
    con = duckdb.connect(":memory:")
    con.execute(f"COPY (SELECT 1 AS a, 'p' AS b) TO '{d / 'p1.parquet'}' (FORMAT parquet)")
    con.execute(f"COPY (SELECT 2 AS a, 'q' AS b) TO '{d / 'p2.parquet'}' (FORMAT parquet)")
    con.close()
    return d


def _add(name: str, sql: str) -> None:
    res = runner.invoke(app, ["add", "-n", name, "-q", sql])
    assert res.exit_code == 0, res.stdout


# --------------------------------------------------------------------------
# Unit: helpers
# --------------------------------------------------------------------------


def test_is_glob() -> None:
    assert is_glob("logs/*.csv")
    assert is_glob("a?.csv")
    assert is_glob("f[0-9].csv")
    assert not is_glob("logs/plain.csv")
    assert not is_glob(None)


def test_expand_glob_sorted_files_only(csv_dir: Path) -> None:
    files = expand_glob(str(csv_dir / "*.csv"))
    assert [p.name for p in files] == ["one.csv", "two.csv"]
    # A directory-matching glob yields no *files*.
    assert expand_glob(str(csv_dir.parent / "logs")) == []


# --------------------------------------------------------------------------
# Engine: run_query_multi
# --------------------------------------------------------------------------


@needs_duckdb
def test_multi_union(csv_dir: Path) -> None:
    run = run_query_multi("select * from data", pattern=str(csv_dir / "*.csv"))
    assert run.file_count == 2
    assert run.combined is not None
    assert run.combined.columns == ["a", "b"]
    assert run.combined.rowcount == 3


@needs_duckdb
def test_multi_per_file(csv_dir: Path) -> None:
    run = run_query_multi(
        "select * from data", pattern=str(csv_dir / "*.csv"), per_file=True
    )
    assert run.combined is None
    assert [p.name for p, _ in run.per_file] == ["one.csv", "two.csv"]
    assert run.per_file[0][1].rowcount == 2
    assert run.per_file[1][1].rowcount == 1


@needs_duckdb
def test_multi_with_source(csv_dir: Path) -> None:
    run = run_query_multi(
        "select * from data",
        pattern=str(csv_dir / "*.csv"),
        with_source=True,
    )
    assert run.combined is not None
    assert run.combined.columns[0] == SOURCE_COLUMN
    # Every row carries its originating path.
    sources = {row[0] for row in run.combined.rows}
    assert any(s.endswith("one.csv") for s in sources)
    assert any(s.endswith("two.csv") for s in sources)


@needs_duckdb
def test_multi_parquet(parquet_dir: Path) -> None:
    run = run_query_multi(
        "select * from data order by a", pattern=str(parquet_dir / "*.parquet")
    )
    assert run.combined is not None
    assert run.combined.rowcount == 2
    assert [r[0] for r in run.combined.rows] == [1, 2]


def test_multi_empty_glob_errors(tmp_path: Path) -> None:
    with pytest.raises(EngineError) as exc:
        run_query_multi("select 1", pattern=str(tmp_path / "*.nope"))
    assert "no files" in str(exc.value)


@needs_duckdb
def test_multi_union_incompatible_columns(tmp_path: Path) -> None:
    d = tmp_path / "mixed"
    d.mkdir()
    (d / "a1.csv").write_text("a,b\n1,x\n", encoding="utf-8")
    (d / "a2.csv").write_text("c,d\n2,y\n", encoding="utf-8")
    with pytest.raises(EngineError) as exc:
        run_query_multi("select * from data", pattern=str(d / "*.csv"))
    assert "different columns" in str(exc.value)


# --------------------------------------------------------------------------
# CLI: run --file '<glob>'
# --------------------------------------------------------------------------


@needs_duckdb
def test_cli_run_glob_union(csv_dir: Path) -> None:
    _add("allrows", "select * from data order by a")
    res = runner.invoke(
        app, ["run", "allrows", "--file", str(csv_dir / "*.csv"), "-F", "csv"]
    )
    assert res.exit_code == 0, res.stdout
    lines = [l for l in res.stdout.strip().splitlines() if l]
    assert lines[0] == "a,b"
    assert "1,x" in lines and "3,z" in lines
    # Three data rows unioned.
    assert len(lines) == 4


@needs_duckdb
def test_cli_run_glob_per_file_with_source(csv_dir: Path) -> None:
    _add("allrows", "select * from data order by a")
    res = runner.invoke(
        app,
        [
            "run",
            "allrows",
            "--file",
            str(csv_dir / "*.csv"),
            "--per-file",
            "--with-source",
            "-F",
            "csv",
        ],
    )
    assert res.exit_code == 0, res.stdout
    out = res.stdout
    assert "one.csv" in out and "two.csv" in out
    assert SOURCE_COLUMN in out


@needs_duckdb
def test_cli_run_glob_empty_errors(tmp_path: Path) -> None:
    _add("allrows", "select * from data")
    res = runner.invoke(
        app, ["run", "allrows", "--file", str(tmp_path / "*.parquet")]
    )
    assert res.exit_code != 0
    assert "no files" in res.stderr


@needs_duckdb
def test_cli_per_file_requires_glob(csv_dir: Path) -> None:
    _add("allrows", "select * from data")
    res = runner.invoke(
        app,
        ["run", "allrows", "--file", str(csv_dir / "one.csv"), "--per-file"],
    )
    assert res.exit_code != 0
    assert "glob" in res.stderr
