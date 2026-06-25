"""Tests for :mod:`quackpack.params` placeholder detection."""

from __future__ import annotations

import pytest

from quackpack.params import (
    coerce_value,
    extract_params,
    split_param_key,
    to_duckdb_placeholders,
)


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


# -- coerce_value -----------------------------------------------------------


def test_coerce_auto_int() -> None:
    assert coerce_value("42") == 42
    assert isinstance(coerce_value("42"), int)


def test_coerce_auto_negative_int() -> None:
    assert coerce_value("-7") == -7
    assert isinstance(coerce_value("-7"), int)


def test_coerce_auto_float() -> None:
    assert coerce_value("3.14") == 3.14
    assert isinstance(coerce_value("3.14"), float)


def test_coerce_auto_float_scientific() -> None:
    assert coerce_value("1e3") == 1000.0
    assert isinstance(coerce_value("1e3"), float)


def test_coerce_auto_string_stays_string() -> None:
    assert coerce_value("west") == "west"


def test_coerce_whitespace_is_trimmed_before_typing() -> None:
    assert coerce_value("  10  ") == 10


def test_coerce_empty_string_stays_string() -> None:
    assert coerce_value("") == ""


def test_coerce_str_hint_forces_text() -> None:
    # Leading-zero ids must not become ints.
    assert coerce_value("007", "str") == "007"


def test_coerce_int_hint() -> None:
    assert coerce_value("5", "int") == 5
    assert isinstance(coerce_value("5", "int"), int)


def test_coerce_float_hint_promotes_int_literal() -> None:
    assert coerce_value("5", "float") == 5.0
    assert isinstance(coerce_value("5", "float"), float)


def test_coerce_int_hint_rejects_float() -> None:
    with pytest.raises(ValueError):
        coerce_value("3.5", "int")


def test_coerce_int_hint_rejects_text() -> None:
    with pytest.raises(ValueError):
        coerce_value("abc", "int")


def test_coerce_non_string_passthrough() -> None:
    assert coerce_value(10) == 10
    assert coerce_value(2.5) == 2.5


@pytest.mark.parametrize("text", ["inf", "Inf", "-inf", "infinity", "nan", "NaN"])
def test_coerce_non_finite_stay_strings_by_default(text: str) -> None:
    # A literal "nan"/"inf" param almost always means the string, not IEEE.
    assert coerce_value(text) == text
    assert isinstance(coerce_value(text), str)


def test_coerce_float_hint_still_allows_inf() -> None:
    import math

    assert math.isinf(coerce_value("inf", "float"))


# -- split_param_key --------------------------------------------------------


def test_split_key_plain() -> None:
    assert split_param_key("n") == ("n", None)


def test_split_key_with_type_hint() -> None:
    assert split_param_key("n:int") == ("n", "int")
    assert split_param_key("ratio:float") == ("ratio", "float")
    assert split_param_key("zip:str") == ("zip", "str")


def test_split_key_unknown_suffix_kept_whole() -> None:
    # A non-type suffix isn't a hint; the colon stays part of the key.
    assert split_param_key("weird:name") == ("weird:name", None)


def test_split_key_strips_whitespace() -> None:
    assert split_param_key("  n  ") == ("n", None)
    assert split_param_key(" n:int ") == ("n", "int")
