"""Tests for :mod:`quackpack.store` — the YAML catalog.

Each test points ``QUACKPACK_HOME`` at a temp dir (via ``monkeypatch``) so the
real user catalog is never touched and roundtrips are isolated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quackpack.store import (
    Catalog,
    CatalogError,
    DuplicateQueryError,
    Query,
    QueryNotFoundError,
    catalog_path,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("QUACKPACK_HOME", str(tmp_path))
    return tmp_path


def test_catalog_path_honors_env(home: Path) -> None:
    assert catalog_path() == home / "pack.yaml"


def test_query_derives_params_and_normalises() -> None:
    q = Query(
        name="  top  ",
        sql="  select * from t where id = :id and id = :id and x = :x  ",
        tags=["sales", "sales", " adhoc ", ""],
        desc="  biggest  ",
    )
    assert q.name == "top"
    assert q.sql.startswith("select")
    assert q.tags == ["sales", "adhoc"]  # de-duped, trimmed, blanks dropped
    assert q.desc == "biggest"
    assert q.params == ["id", "x"]  # derived, distinct, in order


def test_roundtrip_survives_reload(home: Path) -> None:
    cat = Catalog.load()
    cat.add(Query(name="a", sql="select :n", tags=["t1"], desc="first"))
    cat.add(Query(name="b", sql="select 2", tags=["t2"]))

    # Fresh load from disk == survives "restart".
    again = Catalog.load()
    assert again.names() == ["a", "b"]
    a = again.get("a")
    assert a.sql == "select :n"
    assert a.tags == ["t1"]
    assert a.desc == "first"
    assert a.params == ["n"]
    assert a.created  # timestamp populated


def test_file_is_created_on_save(home: Path) -> None:
    assert not catalog_path().exists()
    Catalog.load().add(Query(name="x", sql="select 1"))
    assert catalog_path().exists()


def test_duplicate_name_raises(home: Path) -> None:
    cat = Catalog.load()
    cat.add(Query(name="dup", sql="select 1"))
    with pytest.raises(DuplicateQueryError):
        cat.add(Query(name="dup", sql="select 2"))


def test_duplicate_overwrite_replaces(home: Path) -> None:
    cat = Catalog.load()
    cat.add(Query(name="dup", sql="select 1"))
    cat.add(Query(name="dup", sql="select 999"), overwrite=True)
    assert len(cat) == 1
    assert Catalog.load().get("dup").sql == "select 999"


def test_empty_name_or_sql_rejected(home: Path) -> None:
    cat = Catalog.load()
    with pytest.raises(CatalogError):
        cat.add(Query(name="", sql="select 1"))
    with pytest.raises(CatalogError):
        cat.add(Query(name="ok", sql="   "))


def test_get_missing_raises(home: Path) -> None:
    with pytest.raises(QueryNotFoundError):
        Catalog.load().get("nope")


def test_remove(home: Path) -> None:
    cat = Catalog.load()
    cat.add(Query(name="gone", sql="select 1"))
    removed = cat.remove("gone")
    assert removed.name == "gone"
    assert Catalog.load().names() == []
    with pytest.raises(QueryNotFoundError):
        cat.remove("gone")


def test_list_sorted_and_tag_filter(home: Path) -> None:
    cat = Catalog.load()
    cat.add(Query(name="zeta", sql="select 1", tags=["x"]))
    cat.add(Query(name="alpha", sql="select 2", tags=["x", "y"]))
    cat.add(Query(name="mid", sql="select 3", tags=["y"]))

    assert [q.name for q in cat.list()] == ["alpha", "mid", "zeta"]
    assert [q.name for q in cat.list(tag="x")] == ["alpha", "zeta"]
    assert [q.name for q in cat.list(tag="y")] == ["alpha", "mid"]


def test_search_across_fields(home: Path) -> None:
    cat = Catalog.load()
    cat.add(Query(name="orders", sql="select * from orders", tags=["sales"], desc="all orders"))
    cat.add(Query(name="users", sql="select * from users", tags=["auth"], desc="people"))

    assert [q.name for q in cat.search("order")] == ["orders"]  # name+sql+desc
    assert [q.name for q in cat.search("auth")] == ["users"]  # tag
    assert [q.name for q in cat.search("select")] == ["orders", "users"]  # sql, sorted
    assert cat.search("zzz") == []


def test_malformed_catalog_raises(home: Path) -> None:
    catalog_path().parent.mkdir(parents=True, exist_ok=True)
    catalog_path().write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(CatalogError):
        Catalog.load()


# --------------------------------------------------------------------------
# M5: run history + update (edit backing)
# --------------------------------------------------------------------------


def test_query_history_defaults_and_roundtrip(home: Path) -> None:
    cat = Catalog.load()
    cat.add(Query(name="h", sql="select 1"))
    q = Catalog.load().get("h")
    # Fresh query: never run.
    assert q.run_count == 0
    assert q.last_run == ""
    assert q.last_status == ""


def test_record_run_bumps_and_persists(home: Path) -> None:
    cat = Catalog.load()
    cat.add(Query(name="h", sql="select 1"))

    cat.record_run("h", "ok")
    cat.record_run("h", "error")

    # Survives reload.
    q = Catalog.load().get("h")
    assert q.run_count == 2
    assert q.last_status == "error"
    assert q.last_run  # timestamp set


def test_record_run_missing_raises(home: Path) -> None:
    with pytest.raises(QueryNotFoundError):
        Catalog.load().record_run("nope")


def test_record_run_in_place_query_mutates() -> None:
    q = Query(name="h", sql="select 1")
    q.record_run("ok", when="2026-06-25T12:00:00+00:00")
    assert q.run_count == 1
    assert q.last_run == "2026-06-25T12:00:00+00:00"
    assert q.last_status == "ok"


def test_garbled_history_fields_tolerated(home: Path) -> None:
    # A hand-edited catalog with junk in the history fields should load cleanly,
    # coercing run_count to a sane non-negative int and blanking the rest.
    catalog_path().parent.mkdir(parents=True, exist_ok=True)
    catalog_path().write_text(
        "version: 1\n"
        "queries:\n"
        "  - name: h\n"
        "    sql: select 1\n"
        "    run_count: not-a-number\n"
        "    last_run: null\n",
        encoding="utf-8",
    )
    q = Catalog.load().get("h")
    assert q.run_count == 0
    assert q.last_run == ""
    assert q.last_status == ""


def test_update_replaces_in_place_preserving_order(home: Path) -> None:
    cat = Catalog.load()
    cat.add(Query(name="a", sql="select 1"))
    cat.add(Query(name="b", sql="select 2"))

    cat.update(Query(name="a", sql="select 999 where x = :x"))

    again = Catalog.load()
    assert again.names() == ["a", "b"]  # order kept
    a = again.get("a")
    assert a.sql == "select 999 where x = :x"
    assert a.params == ["x"]  # re-derived from new SQL


def test_update_missing_raises(home: Path) -> None:
    with pytest.raises(QueryNotFoundError):
        Catalog.load().update(Query(name="ghost", sql="select 1"))


def test_update_rejects_empty(home: Path) -> None:
    cat = Catalog.load()
    cat.add(Query(name="a", sql="select 1"))
    with pytest.raises(CatalogError):
        cat.update(Query(name="", sql="select 1"))
