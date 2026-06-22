"""Tests for :mod:`quackpack.params` placeholder detection."""

from __future__ import annotations

from quackpack.params import extract_params


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
