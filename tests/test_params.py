"""Tests for :mod:`quackpack.params` placeholder detection."""

from __future__ import annotations

from quackpack.params import extract_params, to_duckdb_placeholders


def test_basic_params_in_order() -> None:
    sql = "select * from t where id = :id and ts > :since and id <> :id"
    assert extract_params(sql) == ["id", "since"]


def test_no_params() -> None:
    assert extract_params("select 1, 2, 3") == []


def test_cast_double_colon_is_not_a_param() -> None:
    assert extract_params("select x::int, y::varchar from t") == []


def test_colon_inside_string_literal_ignored() -> None:
    sql = "select '12:30' as label, :real_one from t"
    assert extract_params(sql) == ["real_one"]


def test_colon_inside_quoted_identifier_ignored() -> None:
    sql = 'select "weird:col" from t where a = :a'
    assert extract_params(sql) == ["a"]


def test_underscores_and_digits_allowed() -> None:
    assert extract_params("select :p_1, :p2 from t") == ["p_1", "p2"]


# -- to_duckdb_placeholders -------------------------------------------------


def test_duckdb_rewrite_basic() -> None:
    assert (
        to_duckdb_placeholders("select * from t where id = :id")
        == "select * from t where id = $id"
    )


def test_duckdb_rewrite_multiple_and_repeat() -> None:
    sql = "select * from t where a = :a and b > :b and a2 = :a"
    assert (
        to_duckdb_placeholders(sql)
        == "select * from t where a = $a and b > $b and a2 = $a"
    )


def test_duckdb_rewrite_leaves_casts_alone() -> None:
    assert to_duckdb_placeholders("select 1::int as x, :n") == "select 1::int as x, $n"


def test_duckdb_rewrite_leaves_string_literals_alone() -> None:
    sql = "select '12:30' as t, :real_one"
    assert to_duckdb_placeholders(sql) == "select '12:30' as t, $real_one"


def test_duckdb_rewrite_no_params_is_identity() -> None:
    sql = "select count(*) from t"
    assert to_duckdb_placeholders(sql) == sql
