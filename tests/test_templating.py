"""Unit tests for query templating / composition (backlog #10).

Exercises the pure expansion core in :mod:`quackpack.templating`:

* ``extract_refs`` / ``has_refs`` detect ``{{ ref }}`` tokens and stay
  literal-safe (references inside quoted strings/identifiers are ignored);
* ``expand`` inlines referenced queries as parenthesised subqueries, preserves
  ``:param`` placeholders, and resolves multi-level nesting;
* cycles (direct, transitive, self) raise :class:`TemplateCycleError` with the
  offending path, and unknown references raise :class:`TemplateNotFoundError`;
* ``expand_query`` binds a catalog-like object and reports the same errors.

Everything here is dependency-free (no DuckDB, no catalog file); ``expand`` is
driven by a plain ``dict.__getitem__`` resolver.
"""

from __future__ import annotations

import pytest

from quackpack.templating import (
    TemplateCycleError,
    TemplateNotFoundError,
    expand,
    expand_query,
    extract_refs,
    has_refs,
)


# --------------------------------------------------------------------------
# extract_refs / has_refs
# --------------------------------------------------------------------------


def test_extract_refs_basic_order_and_dedupe() -> None:
    sql = "select * from {{ base }} join {{ dims }} using (id) join {{ base }} b2"
    assert extract_refs(sql) == ["base", "dims"]


def test_extract_refs_allows_hyphenated_names() -> None:
    assert extract_refs("select * from {{ top-regions }}") == ["top-regions"]


def test_extract_refs_tolerates_inner_whitespace() -> None:
    assert extract_refs("select * from {{   spaced   }}") == ["spaced"]


def test_extract_refs_none() -> None:
    assert extract_refs("select 1") == []


def test_extract_refs_is_literal_safe() -> None:
    # A ref inside a single-quoted string or double-quoted identifier is text.
    sql = "select '{{ nope }}' as a, \"{{ ignored }}\" from {{ real }}"
    assert extract_refs(sql) == ["real"]


def test_has_refs() -> None:
    assert has_refs("select * from {{ base }}") is True
    assert has_refs("select '{{ x }}'") is False
    assert has_refs("select 1") is False


# --------------------------------------------------------------------------
# expand
# --------------------------------------------------------------------------


def test_expand_no_refs_is_identity() -> None:
    sql = "select * from t where id = :id"
    assert expand(sql, {}.__getitem__) == sql


def test_expand_single_ref_wraps_in_parens_and_keeps_params() -> None:
    pack = {
        "base": "select * from read_csv_auto(:file) where ok",
        "wrap": "select * from {{ base }} limit :n",
    }
    assert (
        expand(pack["wrap"], pack.__getitem__)
        == "select * from (select * from read_csv_auto(:file) where ok) limit :n"
    )


def test_expand_multiple_refs() -> None:
    pack = {
        "a": "select 1 as x",
        "b": "select 2 as y",
        "top": "select * from {{ a }}, {{ b }}",
    }
    assert (
        expand(pack["top"], pack.__getitem__)
        == "select * from (select 1 as x), (select 2 as y)"
    )


def test_expand_nested_multi_level() -> None:
    pack = {
        "l0": "select * from raw",
        "l1": "select * from {{ l0 }} where a",
        "l2": "select * from {{ l1 }} where b",
    }
    assert (
        expand(pack["l2"], pack.__getitem__)
        == "select * from (select * from (select * from raw) where a) where b"
    )


def test_expand_same_ref_twice_inlines_both() -> None:
    pack = {
        "u": "select id from users",
        "pair": "select * from {{ u }} a join {{ u }} b on a.id = b.id",
    }
    assert (
        expand(pack["pair"], pack.__getitem__)
        == "select * from (select id from users) a join (select id from users) b on a.id = b.id"
    )


def test_expand_is_literal_safe() -> None:
    # The braces in the string literal must survive untouched, and only the
    # real ref is expanded.
    pack = {"base": "select 1"}
    sql = "select '{{ base }}' as label, * from {{ base }}"
    assert (
        expand(sql, pack.__getitem__)
        == "select '{{ base }}' as label, * from (select 1)"
    )


def test_expand_unknown_ref_raises_not_found() -> None:
    with pytest.raises(TemplateNotFoundError) as ei:
        expand("select * from {{ ghost }}", {}.__getitem__)
    assert "ghost" in str(ei.value)


# --------------------------------------------------------------------------
# cycle detection
# --------------------------------------------------------------------------


def test_expand_direct_self_cycle() -> None:
    pack = {"me": "select * from {{ me }}"}
    with pytest.raises(TemplateCycleError) as ei:
        expand(pack["me"], pack.__getitem__, _stack=("me",))
    assert "me -> me" in str(ei.value)


def test_expand_transitive_cycle_reports_path() -> None:
    pack = {
        "a": "select * from {{ b }}",
        "b": "select * from {{ c }}",
        "c": "select * from {{ a }}",
    }
    with pytest.raises(TemplateCycleError) as ei:
        expand(pack["a"], pack.__getitem__, _stack=("a",))
    assert "a -> b -> c -> a" in str(ei.value)


# --------------------------------------------------------------------------
# expand_query (catalog-bound convenience)
# --------------------------------------------------------------------------


class _Q:
    def __init__(self, sql: str) -> None:
        self.sql = sql


class _FakeCatalog:
    """Minimal catalog stand-in: get(name) -> object with .sql, else raise."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._m = mapping

    def get(self, name: str):
        from quackpack.store import QueryNotFoundError

        try:
            return _Q(self._m[name])
        except KeyError:
            raise QueryNotFoundError(f"No query named {name!r}.") from None


def test_expand_query_resolves_via_catalog() -> None:
    cat = _FakeCatalog(
        {
            "clean": "select * from raw where ok",
            "report": "select * from {{ clean }} limit :n",
        }
    )
    assert (
        expand_query(cat, "report")
        == "select * from (select * from raw where ok) limit :n"
    )


def test_expand_query_missing_ref_raises_not_found() -> None:
    cat = _FakeCatalog({"report": "select * from {{ missing }}"})
    with pytest.raises(TemplateNotFoundError):
        expand_query(cat, "report")


def test_expand_query_self_cycle_reports_root() -> None:
    cat = _FakeCatalog({"loop": "select * from {{ loop }}"})
    with pytest.raises(TemplateCycleError) as ei:
        expand_query(cat, "loop")
    assert "loop -> loop" in str(ei.value)
