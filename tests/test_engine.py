"""Unit tests for the execution engine (quackpack.engine).

Exercises both backends directly against tiny on-disk fixtures:

* DuckDB — CSV and Parquet reads, ``:param`` binding (translated to ``$param``),
  ``--db`` against a DuckDB file, and clean errors.
* SQLite fallback — CSV ingest with inferred numeric affinity, ``.sqlite``
  ``--db``/``--file``, Parquet rejection.

Parquet fixtures are produced with DuckDB itself (already a dependency), so no
extra tooling is required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quackpack import engine
from quackpack.engine import EngineError, QueryResult, run_query

pytestmark = pytest.mark.filterwarnings("ignore")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def sales_csv(tmp_path: Path) -> Path:
    p = tmp_path / "sales.csv"
    p.write_text(
        "region,amount\nwest,100\neast,250\nwest,75\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def sales_parquet(tmp_path: Path, sales_csv: Path) -> Path:
    duckdb = pytest.importorskip("duckdb")
    out = tmp_path / "sales.parquet"
    con = duckdb.connect(":memory:")
    # COPY ... TO doesn't accept bound parameters for paths; build the relation
    # from the CSV (bound) then write to a literal path.
    con.execute("CREATE TABLE sales AS SELECT * FROM read_csv_auto(?)", [str(sales_csv)])
    con.execute(f"COPY sales TO '{out}' (FORMAT parquet)")
    con.close()
    return out


@pytest.fixture
def shop_sqlite(tmp_path: Path) -> Path:
    import sqlite3

    p = tmp_path / "shop.sqlite"
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE orders (id INTEGER, total REAL)")
    con.executemany(
        "INSERT INTO orders VALUES (?, ?)", [(1, 9.5), (2, 20.0), (3, 4.25)]
    )
    con.commit()
    con.close()
    return p


@pytest.fixture
def shop_duckdb(tmp_path: Path) -> Path:
    duckdb = pytest.importorskip("duckdb")
    p = tmp_path / "shop.duckdb"
    con = duckdb.connect(str(p))
    con.execute(
        "CREATE TABLE orders AS "
        "SELECT * FROM (VALUES (1, 9.5), (2, 20.0), (3, 4.25)) t(id, total)"
    )
    con.close()
    return p


def _as_dicts(result: QueryResult) -> list[dict]:
    return [dict(zip(result.columns, row)) for row in result.rows]


# --------------------------------------------------------------------------
# DuckDB backend
# --------------------------------------------------------------------------


@pytest.mark.skipif(not engine.DUCKDB_AVAILABLE, reason="duckdb not installed")
class TestDuckDB:
    def test_csv_aggregate_by_stem_name(self, sales_csv: Path) -> None:
        res = run_query(
            "select region, sum(amount) as total from sales group by region order by region",
            file=sales_csv,
            engine="duckdb",
        )
        assert res.columns == ["region", "total"]
        assert _as_dicts(res) == [
            {"region": "east", "total": 250},
            {"region": "west", "total": 175},
        ]

    def test_parquet_read(self, sales_parquet: Path) -> None:
        res = run_query(
            "select count(*) as n from sales",
            file=sales_parquet,
            engine="duckdb",
        )
        assert res.rows == [(3,)]

    def test_param_binding_is_typed(self, sales_csv: Path) -> None:
        # ``:min`` is rewritten to ``$min`` and bound numerically, so the
        # comparison is integer-correct (not lexical).
        res = run_query(
            "select amount from sales where amount > :min order by amount",
            file=sales_csv,
            params={"min": 90},
            engine="duckdb",
        )
        assert res.rows == [(100,), (250,)]

    def test_param_colon_in_string_is_not_bound(self, sales_csv: Path) -> None:
        res = run_query(
            "select '12:30' as label, :n as given",
            file=sales_csv,
            params={"n": 7},
            engine="duckdb",
        )
        assert res.rows == [("12:30", 7)]

    def test_db_file(self, shop_duckdb: Path) -> None:
        res = run_query(
            "select id from orders where total > :t order by id",
            db=shop_duckdb,
            params={"t": 5},
            engine="duckdb",
        )
        assert res.rows == [(1,), (2,)]

    def test_sql_error_is_wrapped(self, sales_csv: Path) -> None:
        with pytest.raises(EngineError) as ei:
            run_query("select * from", file=sales_csv, engine="duckdb")
        assert "SQL error" in str(ei.value)

    def test_missing_file_errors(self, tmp_path: Path) -> None:
        with pytest.raises(EngineError) as ei:
            run_query("select 1", file=tmp_path / "nope.csv", engine="duckdb")
        assert "not found" in str(ei.value)

    def test_no_file_still_runs_pure_sql(self) -> None:
        res = run_query("select 1 + 1 as two", engine="duckdb")
        assert res.rows == [(2,)]


# --------------------------------------------------------------------------
# SQLite backend
# --------------------------------------------------------------------------


class TestSQLite:
    def test_csv_ingest_numeric_affinity(self, sales_csv: Path) -> None:
        # Numeric column inferred as INTEGER -> numeric comparison works.
        res = run_query(
            "select amount from sales where amount > :min order by amount",
            file=sales_csv,
            params={"min": 90},
            engine="sqlite",
        )
        assert res.rows == [(100,), (250,)]

    def test_csv_aggregate(self, sales_csv: Path) -> None:
        res = run_query(
            "select region, sum(amount) as total from sales group by region order by region",
            file=sales_csv,
            engine="sqlite",
        )
        assert _as_dicts(res) == [
            {"region": "east", "total": 250},
            {"region": "west", "total": 175},
        ]

    def test_sqlite_db_file(self, shop_sqlite: Path) -> None:
        res = run_query(
            "select id from orders where total > :t order by id",
            db=shop_sqlite,
            params={"t": 5},
            engine="sqlite",
        )
        assert res.rows == [(1,), (2,)]

    def test_sqlite_file_as_data(self, shop_sqlite: Path) -> None:
        res = run_query(
            "select count(*) as n from orders",
            file=shop_sqlite,
            engine="sqlite",
        )
        assert res.rows == [(3,)]

    def test_parquet_rejected(self, sales_parquet: Path) -> None:
        if not engine.DUCKDB_AVAILABLE:
            pytest.skip("need duckdb to build the parquet fixture")
        with pytest.raises(EngineError) as ei:
            run_query("select 1", file=sales_parquet, engine="sqlite")
        assert "Parquet" in str(ei.value)

    def test_sql_error_is_wrapped(self, sales_csv: Path) -> None:
        with pytest.raises(EngineError) as ei:
            run_query("select * from nonexistent_table", file=sales_csv, engine="sqlite")
        assert "SQL error" in str(ei.value)


# --------------------------------------------------------------------------
# Engine resolution / misc
# --------------------------------------------------------------------------


def test_empty_query_rejected() -> None:
    with pytest.raises(EngineError):
        run_query("   ")


def test_unknown_engine_rejected() -> None:
    with pytest.raises(EngineError) as ei:
        run_query("select 1", engine="postgres")
    assert "Unknown engine" in str(ei.value)


def test_available_engines_includes_sqlite() -> None:
    assert "sqlite" in engine.available_engines()


def test_auto_prefers_duckdb_when_present() -> None:
    res = run_query("select 1 as x", engine="auto")
    assert res.rows == [(1,)]
