"""Unit tests for the static SQL lints (quackpack.lints)."""

from __future__ import annotations

from quackpack.lints import lint_sql


def test_select_star_triggers_lint() -> None:
    warnings = lint_sql("SELECT * FROM sales")
    assert any("SELECT *" in w for w in warnings)


def test_qualified_star_triggers_lint() -> None:
    assert any("SELECT *" in w for w in lint_sql("select s.* from sales s"))


def test_explicit_columns_no_star_lint() -> None:
    warnings = lint_sql("SELECT region, amount FROM sales")
    assert not any("SELECT *" in w for w in warnings)


def test_count_star_not_flagged() -> None:
    # count(*) is not a broad projection; must not trip the SELECT * lint.
    assert not any("SELECT *" in w for w in lint_sql("SELECT count(*) FROM sales"))


def test_unfiltered_file_scan_triggers_lint() -> None:
    sql = "SELECT a, b FROM read_csv_auto('big.csv')"
    assert any("full-file scan" in w for w in lint_sql(sql))


def test_filtered_file_scan_ok() -> None:
    sql = "SELECT a FROM read_parquet('big.parquet') WHERE a > 10"
    assert not any("full-file scan" in w for w in lint_sql(sql))


def test_limited_file_scan_ok() -> None:
    sql = "SELECT a FROM read_csv_auto('big.csv') LIMIT 5"
    assert not any("full-file scan" in w for w in lint_sql(sql))


def test_comments_ignored() -> None:
    # A SELECT * hidden in a comment must not trigger a lint.
    sql = "-- SELECT * FROM x\nSELECT a FROM sales"
    assert lint_sql(sql) == []


def test_clean_query_no_warnings() -> None:
    assert lint_sql("SELECT region FROM sales WHERE amount > 1") == []
