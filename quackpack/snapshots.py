"""Result snapshots & diff for quackpack (backlog #3).

`quackpack run` caches the *result* of a query so a later `quackpack diff`
can answer the one question this feature exists for: **"what changed since
the last time I ran this?"** — added rows, removed rows, and (when a key is
declared) rows whose non-key values changed. It is a deliberately small,
local-first data-drift / regression spot check, *not* a BI or dashboards
feature: no charts, no history beyond the single last run.

Where snapshots live
--------------------
Each query gets one JSON sidecar under ``$QUACKPACK_HOME/snapshots/`` (kept
separate from ``pack.yaml`` so cached data never bloats or risks corrupting the
catalog). The filename is the query name run through a filesystem-safe slug plus
a short hash of the exact name, so two queries whose names slug identically
(``a/b`` vs ``a b``) never collide::

    $QUACKPACK_HOME/snapshots/top-customers-1a2b3c4d.json

File shape::

    {
      "version": 1,
      "query": "top-customers",
      "taken": "2026-07-07T19:40:00+00:00",
      "engine": "duckdb",
      "params": {"n": 25},
      "key": ["id"],
      "columns": ["id", "name", "spend"],
      "rows": [[1, "acme", 900], [2, "globex", 750]]
    }

Why pure diff functions
-----------------------
The diff itself (:func:`diff_results`) is a pure function over two
:class:`~quackpack.engine.QueryResult`-shaped inputs, so it is trivial to
unit-test in isolation and the CLI just loads, re-runs, diffs, and renders. Row
identity is either an explicit *key* (a subset of columns, like a primary key)
or — with no key — the whole row. With a key, rows sharing a key but differing
elsewhere are reported as **changed** (with per-column before/after); without a
key we can only meaningfully report **added** / **removed**.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .engine import QueryResult
from .history import now_iso
from .store import catalog_home

__all__ = [
    "SNAPSHOTS_VERSION",
    "Snapshot",
    "SnapshotError",
    "RowChange",
    "DiffResult",
    "snapshots_dir",
    "snapshot_path",
    "save_snapshot",
    "load_snapshot",
    "delete_snapshot",
    "diff_results",
]

SNAPSHOTS_VERSION = 1

# Collapse anything that isn't a friendly filename char into a single dash so a
# query named ``sales: q3 2026`` slugs to ``sales-q3-2026``.
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


class SnapshotError(Exception):
    """A user-facing snapshot problem (unreadable/garbled cache file)."""


# --------------------------------------------------------------------------
# Location helpers
# --------------------------------------------------------------------------


def _slug(name: str) -> str:
    """Filesystem-safe stem for *name*: a slug plus a short hash of the exact name.

    The hash suffix guarantees uniqueness even when two different names slug to
    the same text (``a/b`` and ``a b`` both slug to ``a-b``), so snapshots never
    clobber each other.

    >>> _slug("top-customers").startswith("top-customers-")
    True
    >>> _slug("a/b") != _slug("a b")
    True
    """
    base = _SLUG_RE.sub("-", name.strip()).strip("-._")
    if not base:
        base = "query"
    digest = hashlib.sha1(name.strip().encode("utf-8")).hexdigest()[:8]
    # Keep the readable part bounded so the filename stays sane for long names.
    return f"{base[:60]}-{digest}"


def snapshots_dir() -> Path:
    """Directory holding per-query snapshot sidecars (``$QUACKPACK_HOME/snapshots``)."""
    return catalog_home() / "snapshots"


def snapshot_path(name: str) -> Path:
    """Full path to the snapshot file for the query named *name*."""
    return snapshots_dir() / f"{_slug(name)}.json"


# --------------------------------------------------------------------------
# Snapshot record
# --------------------------------------------------------------------------


@dataclass
class Snapshot:
    """A cached query result plus the context needed to diff a later run.

    ``columns`` / ``rows`` mirror a :class:`~quackpack.engine.QueryResult`;
    ``key`` records the identity columns used (empty = whole-row identity).
    ``params`` / ``engine`` are stored for display so ``diff`` can note when the
    current run used different bindings than the snapshot it compares against.
    """

    query: str
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    key: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    engine: str = ""
    taken: str = field(default_factory=now_iso)

    @classmethod
    def from_result(
        cls,
        query: str,
        result: QueryResult,
        *,
        key: Optional[Sequence[str]] = None,
        params: Optional[dict[str, Any]] = None,
        engine: str = "",
        taken: Optional[str] = None,
    ) -> "Snapshot":
        """Build a snapshot from a freshly-run *result*."""
        return cls(
            query=query,
            columns=list(result.columns),
            rows=[tuple(r) for r in result.rows],
            key=list(key or []),
            params=dict(params or {}),
            engine=engine or "",
            taken=taken or now_iso(),
        )

    def as_result(self) -> QueryResult:
        """Return the cached data as a :class:`QueryResult` (for rendering)."""
        return QueryResult(columns=list(self.columns), rows=[tuple(r) for r in self.rows])

    @property
    def rowcount(self) -> int:
        return len(self.rows)

    def to_dict(self) -> dict:
        # Rows are stored as JSON arrays (tuples aren't a JSON type); everything
        # round-trips back to tuples on load for stable comparisons.
        return {
            "version": SNAPSHOTS_VERSION,
            "query": self.query,
            "taken": self.taken,
            "engine": self.engine,
            "params": self.params,
            "key": list(self.key),
            "columns": list(self.columns),
            "rows": [list(r) for r in self.rows],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Snapshot":
        """Build a snapshot from a loaded JSON mapping (tolerant of extras)."""
        rows_raw = data.get("rows") or []
        rows: list[tuple] = []
        if isinstance(rows_raw, list):
            for r in rows_raw:
                if isinstance(r, (list, tuple)):
                    rows.append(tuple(r))
        params = data.get("params")
        return cls(
            query=str(data.get("query", "") or ""),
            columns=[str(c) for c in (data.get("columns") or [])],
            rows=rows,
            key=[str(k) for k in (data.get("key") or [])],
            params=dict(params) if isinstance(params, dict) else {},
            engine=str(data.get("engine", "") or ""),
            taken=str(data.get("taken", "") or ""),
        )


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def save_snapshot(snap: Snapshot, *, path: Optional[Path] = None) -> Path:
    """Atomically write *snap* to its sidecar, returning the path written.

    Creates the snapshots directory as needed and writes via a temp file +
    replace so a crash never leaves a half-written cache.
    """
    p = Path(path) if path is not None else snapshot_path(snap.query)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(snap.to_dict(), ensure_ascii=False, indent=2, default=_json_default)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)
    return p


def load_snapshot(name: str, *, path: Optional[Path] = None) -> Optional[Snapshot]:
    """Load the snapshot for the query named *name*, or ``None`` if there isn't one.

    A missing file is a normal "never snapshotted" state and returns ``None``. A
    present-but-corrupt file raises :class:`SnapshotError` so the CLI can tell the
    user their cache is unreadable (rather than silently pretending it's empty).
    """
    p = Path(path) if path is not None else snapshot_path(name)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"Could not read snapshot for {name!r}: {exc}")
    if not isinstance(raw, dict):
        raise SnapshotError(f"Malformed snapshot for {name!r}: expected a JSON object.")
    return Snapshot.from_dict(raw)


def delete_snapshot(name: str, *, path: Optional[Path] = None) -> bool:
    """Delete the snapshot for *name*; return ``True`` if a file was removed."""
    p = Path(path) if path is not None else snapshot_path(name)
    if p.exists():
        p.unlink()
        return True
    return False


def _json_default(value: Any) -> Any:
    """Serialise values JSON doesn't handle (bytes -> str, else str())."""
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    return str(value)


# --------------------------------------------------------------------------
# Diff
# --------------------------------------------------------------------------


@dataclass
class RowChange:
    """One row that exists in both snapshots (same key) but changed value(s).

    ``key`` is the identity tuple; ``changes`` maps each differing column to a
    ``(before, after)`` pair. ``before`` / ``after`` are the full row dicts for
    context when rendering.
    """

    key: tuple
    before: dict[str, Any]
    after: dict[str, Any]
    changes: dict[str, tuple[Any, Any]]


@dataclass
class DiffResult:
    """The outcome of diffing a previous snapshot against a current result.

    * ``added`` — rows present now but not in the snapshot (row dicts).
    * ``removed`` — rows in the snapshot but gone now (row dicts).
    * ``changed`` — :class:`RowChange` entries (only when a *key* is used).
    * ``columns`` — the current result's columns (for rendering headers).
    * ``key`` — the identity columns used for the diff (empty = whole-row).
    * ``keyed`` — whether a key-based diff (with change detection) ran.
    """

    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    changed: list[RowChange] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    key: list[str] = field(default_factory=list)
    keyed: bool = False

    @property
    def is_empty(self) -> bool:
        """True when nothing changed between the two results."""
        return not (self.added or self.removed or self.changed)

    def summary(self) -> str:
        """One-line ``+A -R ~C`` style summary of the counts."""
        return (
            f"+{len(self.added)} added, "
            f"-{len(self.removed)} removed, "
            f"~{len(self.changed)} changed"
        )


def _row_dict(columns: Sequence[str], row: Sequence[Any]) -> dict[str, Any]:
    """Zip a row tuple against *columns* into an ordered dict."""
    return {col: row[i] if i < len(row) else None for i, col in enumerate(columns)}


def _hashable(value: Any) -> Any:
    """Make *value* usable as a dict key (lists/dicts -> a stable string form).

    Cell values are usually scalars, but a JSON/array column could yield a list;
    fall back to a repr so grouping never raises on an unhashable cell.
    """
    try:
        hash(value)
        return value
    except TypeError:
        return json.dumps(value, sort_keys=True, default=str)


def _key_of(row_map: dict[str, Any], key_cols: Sequence[str]) -> tuple:
    """Identity tuple for *row_map* using *key_cols* (hashable-normalised)."""
    return tuple(_hashable(row_map.get(col)) for col in key_cols)


def _full_key(row_map: dict[str, Any], columns: Sequence[str]) -> tuple:
    """Whole-row identity tuple (used when no explicit key is given)."""
    return tuple(_hashable(row_map.get(col)) for col in columns)


def diff_results(
    previous: QueryResult,
    current: QueryResult,
    *,
    key: Optional[Sequence[str]] = None,
) -> DiffResult:
    """Diff a *previous* result against the *current* one.

    With no *key*, identity is the whole row, so the diff reports **added** and
    **removed** rows (multiset-aware: a row appearing twice before and once now
    shows one removal). With a *key* (a subset of columns), rows are matched by
    that key and any that share a key but differ elsewhere are reported as
    **changed** with per-column before/after values.

    Raises :class:`SnapshotError` if *key* names a column that isn't present in
    both result sets — a clear signal the schema drifted or the key is wrong.
    """
    key_cols = [k for k in (key or []) if str(k).strip()]
    cur_cols = list(current.columns)
    prev_cols = list(previous.columns)

    if key_cols:
        missing_cur = [k for k in key_cols if k not in cur_cols]
        missing_prev = [k for k in key_cols if k not in prev_cols]
        if missing_cur or missing_prev:
            missing = sorted(set(missing_cur) | set(missing_prev))
            raise SnapshotError(
                "Key column(s) not found in results: " + ", ".join(missing)
            )
        return _diff_keyed(previous, current, key_cols)

    return _diff_unkeyed(previous, current)


def _diff_keyed(
    previous: QueryResult, current: QueryResult, key_cols: Sequence[str]
) -> DiffResult:
    cur_cols = list(current.columns)
    prev_maps = {
        _key_of(m := _row_dict(previous.columns, r), key_cols): m for r in previous.rows
    }
    cur_maps = {
        _key_of(m := _row_dict(cur_cols, r), key_cols): m for r in current.rows
    }

    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: list[RowChange] = []

    for k, cur_map in cur_maps.items():
        if k not in prev_maps:
            added.append(cur_map)
            continue
        prev_map = prev_maps[k]
        # Compare on the union of columns so an added/removed column also counts
        # as a change for that keyed row.
        cols = list(dict.fromkeys([*prev_map.keys(), *cur_map.keys()]))
        deltas: dict[str, tuple[Any, Any]] = {}
        for col in cols:
            if col in key_cols:
                continue
            before = prev_map.get(col)
            after = cur_map.get(col)
            if before != after:
                deltas[col] = (before, after)
        if deltas:
            changed.append(RowChange(key=k, before=prev_map, after=cur_map, changes=deltas))

    for k, prev_map in prev_maps.items():
        if k not in cur_maps:
            removed.append(prev_map)

    return DiffResult(
        added=added,
        removed=removed,
        changed=changed,
        columns=cur_cols,
        key=list(key_cols),
        keyed=True,
    )


def _diff_unkeyed(previous: QueryResult, current: QueryResult) -> DiffResult:
    cur_cols = list(current.columns)
    prev_maps = [_row_dict(previous.columns, r) for r in previous.rows]
    cur_maps = [_row_dict(cur_cols, r) for r in current.rows]

    # Multiset diff on whole-row identity: count occurrences on each side and
    # emit the surplus. This keeps duplicate rows honest (two identical rows
    # before and one now => exactly one removal).
    def _counts(maps: Iterable[dict[str, Any]], cols: Sequence[str]) -> dict[tuple, int]:
        out: dict[tuple, int] = {}
        for m in maps:
            k = _full_key(m, cols)
            out[k] = out.get(k, 0) + 1
        return out

    # Compare on the union of columns so a column rename/add is a full swap
    # (every row removed + re-added) rather than silently matching.
    union_cols = list(dict.fromkeys([*previous.columns, *cur_cols]))
    prev_counts = _counts(prev_maps, union_cols)
    cur_counts = _counts(cur_maps, union_cols)

    # Remember one representative row-map per identity for rendering.
    prev_repr = {_full_key(m, union_cols): m for m in prev_maps}
    cur_repr = {_full_key(m, union_cols): m for m in cur_maps}

    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []

    for k, cnt in cur_counts.items():
        surplus = cnt - prev_counts.get(k, 0)
        for _ in range(max(0, surplus)):
            added.append(cur_repr[k])
    for k, cnt in prev_counts.items():
        surplus = cnt - cur_counts.get(k, 0)
        for _ in range(max(0, surplus)):
            removed.append(prev_repr[k])

    return DiffResult(
        added=added,
        removed=removed,
        changed=[],
        columns=cur_cols,
        key=[],
        keyed=False,
    )
