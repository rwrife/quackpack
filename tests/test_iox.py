"""Unit tests for the export/import core (``quackpack.iox``, backlog #5).

These exercise the *pure* sharing logic directly — selection, sharable
serialisation, dangling-ref detection, document parsing/validation, and the
collision-strategy merge planner — without touching disk or the CLI. The
command-level behaviour (stdout/stderr, exit codes, file/stdin I/O) is covered
separately in ``test_cli_iox.py``.
"""

from __future__ import annotations

import pytest

from quackpack import iox
from quackpack.store import Query


def _q(name: str, sql: str = "select 1", **kw) -> Query:
    return Query(name=name, sql=sql, **kw)


# --------------------------------------------------------------------------
# sharable_dict — what gets shared (and what doesn't)
# --------------------------------------------------------------------------


def test_sharable_dict_keeps_query_and_presets() -> None:
    q = Query(
        name="top",
        sql="select * from t where n > :n",
        tags=["sales"],
        desc="Big rows.",
        presets={"q3": {"n": 25}},
    )
    d = iox.sharable_dict(q)
    assert d["name"] == "top"
    assert d["sql"] == "select * from t where n > :n"
    assert d["tags"] == ["sales"]
    assert d["desc"] == "Big rows."
    assert d["params"] == ["n"]
    assert d["presets"] == {"q3": {"n": 25}}


def test_sharable_dict_drops_run_history() -> None:
    q = _q("r")
    q.record_run("ok")
    q.record_run("error")
    d = iox.sharable_dict(q)
    assert "run_count" not in d
    assert "last_run" not in d
    assert "last_status" not in d
    # A fresh Query built from the shared dict has zeroed history.
    reborn = Query.from_dict(d)
    assert reborn.run_count == 0
    assert reborn.last_run == ""
    assert reborn.last_status == ""


# --------------------------------------------------------------------------
# select_queries / missing_names
# --------------------------------------------------------------------------


def test_select_all_when_no_filters() -> None:
    qs = [_q("a"), _q("b"), _q("c")]
    assert [q.name for q in iox.select_queries(qs)] == ["a", "b", "c"]


def test_select_by_name_preserves_order() -> None:
    qs = [_q("a"), _q("b"), _q("c")]
    got = iox.select_queries(qs, ["c", "a"])
    # Output order follows the *input*, not the argument order (stable/diffable).
    assert [q.name for q in got] == ["a", "c"]


def test_select_by_tag() -> None:
    qs = [_q("a", tags=["x"]), _q("b", tags=["y"]), _q("c", tags=["x", "y"])]
    assert [q.name for q in iox.select_queries(qs, tag="x")] == ["a", "c"]


def test_select_name_and_tag_combine_with_and() -> None:
    qs = [_q("a", tags=["x"]), _q("b", tags=["x"]), _q("c", tags=["y"])]
    got = iox.select_queries(qs, ["a", "c"], tag="x")
    assert [q.name for q in got] == ["a"]  # c is excluded by tag


def test_missing_names_reports_unknowns_in_order() -> None:
    qs = [_q("a"), _q("b")]
    assert iox.missing_names(qs, ["a", "zzz", "b", "yyy", "zzz"]) == ["zzz", "yyy"]


# --------------------------------------------------------------------------
# dangling_refs
# --------------------------------------------------------------------------


def test_dangling_refs_flags_out_of_selection_reference() -> None:
    base = _q("base")
    wrap = _q("wrap", "select * from {{ base }} where n > :n")
    # Exporting only wrap leaves its {{ base }} dangling.
    assert iox.dangling_refs([wrap]) == {"wrap": ["base"]}
    # Exporting both closes it.
    assert iox.dangling_refs([base, wrap]) == {}


def test_dangling_refs_ignores_literals() -> None:
    q = _q("q", "select '{{ not_a_ref }}' as t")
    assert iox.dangling_refs([q]) == {}


# --------------------------------------------------------------------------
# build_export
# --------------------------------------------------------------------------


def test_build_export_shapes_document() -> None:
    qs = [_q("a", tags=["t"]), _q("b")]
    sel = iox.build_export(qs)
    assert sel.document["version"] == iox.EXPORT_VERSION
    assert "exported" in sel.document
    assert [d["name"] for d in sel.document["queries"]] == ["a", "b"]
    assert sel.count == 2


def test_build_export_empty_selection_is_valid() -> None:
    sel = iox.build_export([_q("a")], tag="nope")
    assert sel.count == 0
    assert sel.document["queries"] == []


def test_build_export_reports_dangling() -> None:
    qs = [_q("base"), _q("wrap", "select * from {{ base }}")]
    sel = iox.build_export(qs, ["wrap"])
    assert sel.dangling == {"wrap": ["base"]}


# --------------------------------------------------------------------------
# parse_export — validation
# --------------------------------------------------------------------------


def test_parse_export_roundtrips_build_export() -> None:
    qs = [
        Query(name="a", sql="select :x", presets={"p": {"x": 1}}),
        Query(name="b", sql="select 2", tags=["t"], desc="d"),
    ]
    doc = iox.build_export(qs).document
    parsed = iox.parse_export(doc)
    assert [q.name for q in parsed] == ["a", "b"]
    assert parsed[0].presets == {"p": {"x": 1}}
    assert parsed[1].tags == ["t"]
    assert parsed[1].desc == "d"


def test_parse_export_rejects_non_mapping() -> None:
    with pytest.raises(iox.ImportError_):
        iox.parse_export([1, 2, 3])


def test_parse_export_requires_queries_key() -> None:
    with pytest.raises(iox.ImportError_):
        iox.parse_export({"version": 1})


def test_parse_export_queries_must_be_list() -> None:
    with pytest.raises(iox.ImportError_):
        iox.parse_export({"queries": {"a": 1}})


def test_parse_export_rejects_query_without_name() -> None:
    with pytest.raises(iox.ImportError_):
        iox.parse_export({"queries": [{"sql": "select 1"}]})


def test_parse_export_rejects_empty_sql() -> None:
    with pytest.raises(iox.ImportError_):
        iox.parse_export({"queries": [{"name": "x", "sql": "   "}]})


def test_parse_export_accepts_full_pack_yaml_shape() -> None:
    # A whole pack.yaml (extra top-level keys, history fields on queries) is
    # importable as-is; extras are ignored and history is reset.
    pack = {
        "version": 1,
        "queries": [
            {"name": "a", "sql": "select 1", "run_count": 9, "last_status": "ok"}
        ],
    }
    parsed = iox.parse_export(pack)
    assert parsed[0].name == "a"
    assert parsed[0].run_count == 0  # history dropped on the way through Query


# --------------------------------------------------------------------------
# plan_import — collision strategies
# --------------------------------------------------------------------------


def test_plan_import_adds_non_colliding() -> None:
    plan = iox.plan_import(["existing"], [_q("new1"), _q("new2")])
    assert [q.name for q in plan.to_add] == ["new1", "new2"]
    assert plan.skipped == []
    assert plan.renamed == {}
    assert plan.summary() == "imported 2, skipped 0, renamed 0"


def test_plan_import_skip_default_never_clobbers() -> None:
    plan = iox.plan_import(["dup"], [_q("dup", "select 999"), _q("fresh")])
    assert [q.name for q in plan.to_add] == ["fresh"]
    assert plan.skipped == ["dup"]
    assert plan.overwrite == set()


def test_plan_import_overwrite_replaces() -> None:
    plan = iox.plan_import(["dup"], [_q("dup")], strategy="overwrite")
    assert [q.name for q in plan.to_add] == ["dup"]
    assert plan.overwrite == {"dup"}
    assert plan.skipped == []


def test_plan_import_rename_suffixes() -> None:
    plan = iox.plan_import(["dup"], [_q("dup")], strategy="rename")
    assert [q.name for q in plan.to_add] == ["dup-2"]
    assert plan.renamed == {"dup": "dup-2"}


def test_plan_import_rename_avoids_existing_suffixes() -> None:
    # dup and dup-2 already exist -> next free is dup-3.
    plan = iox.plan_import(["dup", "dup-2"], [_q("dup")], strategy="rename")
    assert plan.renamed == {"dup": "dup-3"}


def test_plan_import_rename_bumps_numeric_suffix_of_incoming() -> None:
    # An incoming name that already ends in -2 bumps to -3, not -2-2.
    plan = iox.plan_import(["report-2"], [_q("report-2")], strategy="rename")
    assert plan.renamed == {"report-2": "report-3"}


def test_plan_import_rename_two_collisions_do_not_clash() -> None:
    # Two incoming "dup"s, both colliding: second must not reuse the first's slot.
    plan = iox.plan_import(
        ["dup"], [_q("dup"), _q("dup")], strategy="rename"
    )
    names = [q.name for q in plan.to_add]
    assert names == ["dup-2", "dup-3"]


def test_plan_import_tag_stamps_provenance() -> None:
    plan = iox.plan_import([], [_q("a", tags=["x"])], tag="from-alice")
    assert plan.to_add[0].tags == ["x", "from-alice"]


def test_plan_import_tag_dedupes() -> None:
    # Stamping a tag the query already has is a no-op (Query normalises).
    plan = iox.plan_import([], [_q("a", tags=["keep"])], tag="keep")
    assert plan.to_add[0].tags == ["keep"]


def test_plan_import_unknown_strategy_raises() -> None:
    with pytest.raises(ValueError):
        iox.plan_import([], [_q("a")], strategy="bogus")


def test_plan_import_preserves_presets_through_merge() -> None:
    incoming = [Query(name="a", sql="select :n", presets={"p": {"n": 5}})]
    plan = iox.plan_import([], incoming)
    assert plan.to_add[0].presets == {"p": {"n": 5}}
