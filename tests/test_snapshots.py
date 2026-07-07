"""Unit tests for the snapshots module (backlog #3).

Exercises the *pure* pieces of ``quackpack.snapshots`` in isolation:

* :class:`Snapshot` serialisation round-trips (incl. tuple<->list rows and
  non-JSON cell values);
* on-disk save/load/delete against a throwaway ``QUACKPACK_HOME``, including the
  "missing = None" and "corrupt = error" contracts;
* :func:`diff_results` for both whole-row (unkeyed) and keyed identity, covering
  added / removed / changed rows, duplicate (multiset) handling, no-change, and
  the bad-key error;
* filename slugging so distinct names never collide.

These are deliberately CLI-free so the diff logic is pinned independently of
Typer wiring.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quackpack.engine import QueryResult
from quackpack.snapshots import (
    Snapshot,
    SnapshotError,
    _slug,
    delete_snapshot,
    diff_results,
    load_snapshot,
    save_snapshot,
    snapshot_path,
    snapshots_dir,
)


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("QUACKPACK_HOME", str(tmp_path))
    return tmp_path


def _qr(columns, rows) -> QueryResult:
    return QueryResult(columns=list(columns), rows=[tuple(r) for r in rows])


# --------------------------------------------------------------------------
# Snapshot serialisation
# --------------------------------------------------------------------------


def test_snapshot_roundtrip_preserves_rows_and_key() -> None:
    result = _qr(["id", "name"], [(1, "acme"), (2, "globex")])
    snap = Snapshot.from_result(
        "top", result, key=["id"], params={"n": 5}, engine="duckdb"
    )
    restored = Snapshot.from_dict(snap.to_dict())
    assert restored.query == "top"
    assert restored.columns == ["id", "name"]
    assert restored.rows == [(1, "acme"), (2, "globex")]  # tuples, not lists
    assert restored.key == ["id"]
    assert restored.params == {"n": 5}
    assert restored.engine == "duckdb"


def test_snapshot_as_result_returns_queryresult() -> None:
    snap = Snapshot(query="q", columns=["a"], rows=[(1,), (2,)])
    res = snap.as_result()
    assert isinstance(res, QueryResult)
    assert res.columns == ["a"]
    assert res.rowcount == 2


def test_snapshot_from_dict_tolerates_garbage() -> None:
    # Non-list rows / missing fields degrade gracefully rather than raising.
    snap = Snapshot.from_dict({"query": "q", "rows": "nope", "columns": None})
    assert snap.query == "q"
    assert snap.rows == []
    assert snap.columns == []


def test_snapshot_handles_non_json_cell_values() -> None:
    # bytes should serialise (utf-8) without blowing up the JSON dump.
    result = _qr(["b"], [(b"hi",)])
    snap = Snapshot.from_result("q", result)
    path = save_snapshot(snap)
    text = path.read_text(encoding="utf-8")
    assert "hi" in text  # decoded, not a crash


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def test_save_and_load_roundtrip() -> None:
    result = _qr(["id", "v"], [(1, "a")])
    snap = Snapshot.from_result("orders", result, key=["id"])
    path = save_snapshot(snap)
    assert path == snapshot_path("orders")
    assert path.exists()

    loaded = load_snapshot("orders")
    assert loaded is not None
    assert loaded.rows == [(1, "a")]
    assert loaded.key == ["id"]


def test_load_missing_returns_none() -> None:
    assert load_snapshot("never-run") is None


def test_load_corrupt_raises() -> None:
    p = snapshot_path("bad")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(SnapshotError):
        load_snapshot("bad")


def test_load_non_object_raises() -> None:
    p = snapshot_path("arr")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(SnapshotError):
        load_snapshot("arr")


def test_delete_snapshot() -> None:
    snap = Snapshot.from_result("gone", _qr(["a"], [(1,)]))
    save_snapshot(snap)
    assert delete_snapshot("gone") is True
    assert delete_snapshot("gone") is False  # already gone
    assert load_snapshot("gone") is None


def test_save_is_atomic_no_tmp_left(home: Path) -> None:
    snap = Snapshot.from_result("atomic", _qr(["a"], [(1,)]))
    save_snapshot(snap)
    leftovers = list(snapshots_dir().glob("*.tmp"))
    assert leftovers == []


# --------------------------------------------------------------------------
# Slugging
# --------------------------------------------------------------------------


def test_slug_is_filesystem_safe_and_unique() -> None:
    # Different names that would collapse to the same readable slug must differ.
    assert _slug("a/b") != _slug("a b")
    # Same name is stable.
    assert _slug("top-customers") == _slug("top-customers")
    # No path separators leak into the stem.
    assert "/" not in _slug("a/b/c")


def test_slug_handles_all_symbol_name() -> None:
    stem = _slug("***")
    assert stem  # never empty
    assert "/" not in stem


# --------------------------------------------------------------------------
# diff_results — unkeyed (whole-row identity)
# --------------------------------------------------------------------------


def test_diff_unkeyed_added_and_removed() -> None:
    prev = _qr(["id", "v"], [(1, "a"), (2, "b")])
    cur = _qr(["id", "v"], [(2, "b"), (3, "c")])
    d = diff_results(prev, cur)
    assert not d.keyed
    assert [r["id"] for r in d.added] == [3]
    assert [r["id"] for r in d.removed] == [1]
    assert d.changed == []
    assert d.summary() == "+1 added, -1 removed, ~0 changed"


def test_diff_unkeyed_no_change_is_empty() -> None:
    r = _qr(["id"], [(1,), (2,)])
    d = diff_results(r, r)
    assert d.is_empty
    assert d.summary() == "+0 added, -0 removed, ~0 changed"


def test_diff_unkeyed_multiset_duplicates() -> None:
    # Two identical rows before, one now => exactly one removal.
    prev = _qr(["v"], [("x",), ("x",)])
    cur = _qr(["v"], [("x",)])
    d = diff_results(prev, cur)
    assert len(d.removed) == 1
    assert d.added == []


def test_diff_unkeyed_value_change_is_add_plus_remove() -> None:
    # Without a key, a changed value looks like a removed + added row.
    prev = _qr(["id", "v"], [(1, "a")])
    cur = _qr(["id", "v"], [(1, "b")])
    d = diff_results(prev, cur)
    assert len(d.added) == 1
    assert len(d.removed) == 1
    assert d.changed == []


def test_diff_unkeyed_column_change_is_full_swap() -> None:
    # A renamed/added column changes every row's identity -> full swap.
    prev = _qr(["a"], [(1,), (2,)])
    cur = _qr(["a", "b"], [(1, 9), (2, 9)])
    d = diff_results(prev, cur)
    assert len(d.added) == 2
    assert len(d.removed) == 2


# --------------------------------------------------------------------------
# diff_results — keyed
# --------------------------------------------------------------------------


def test_diff_keyed_detects_changed_rows() -> None:
    prev = _qr(["id", "v"], [(1, "a"), (2, "b")])
    cur = _qr(["id", "v"], [(1, "a"), (2, "B"), (3, "c")])
    d = diff_results(prev, cur, key=["id"])
    assert d.keyed
    assert d.key == ["id"]
    assert [r["id"] for r in d.added] == [3]
    assert d.removed == []
    assert len(d.changed) == 1
    rc = d.changed[0]
    assert rc.key == (2,)
    assert rc.changes == {"v": ("b", "B")}
    assert d.summary() == "+1 added, -0 removed, ~1 changed"


def test_diff_keyed_removed_row() -> None:
    prev = _qr(["id", "v"], [(1, "a"), (2, "b")])
    cur = _qr(["id", "v"], [(1, "a")])
    d = diff_results(prev, cur, key=["id"])
    assert [r["id"] for r in d.removed] == [2]
    assert d.added == []
    assert d.changed == []


def test_diff_keyed_composite_key() -> None:
    prev = _qr(["a", "b", "v"], [(1, 1, "x"), (1, 2, "y")])
    cur = _qr(["a", "b", "v"], [(1, 1, "x"), (1, 2, "Y")])
    d = diff_results(prev, cur, key=["a", "b"])
    assert len(d.changed) == 1
    assert d.changed[0].key == (1, 2)
    assert d.changed[0].changes == {"v": ("y", "Y")}


def test_diff_keyed_no_change() -> None:
    r = _qr(["id", "v"], [(1, "a")])
    d = diff_results(r, r, key=["id"])
    assert d.is_empty


def test_diff_keyed_missing_key_column_raises() -> None:
    prev = _qr(["id", "v"], [(1, "a")])
    cur = _qr(["id", "v"], [(1, "a")])
    with pytest.raises(SnapshotError):
        diff_results(prev, cur, key=["nope"])


def test_diff_keyed_new_column_counts_as_change() -> None:
    # Same key rows, but current has an extra column value -> a change on it.
    prev = _qr(["id", "v"], [(1, "a")])
    cur = _qr(["id", "v", "w"], [(1, "a", "new")])
    d = diff_results(prev, cur, key=["id"])
    assert len(d.changed) == 1
    assert "w" in d.changed[0].changes
