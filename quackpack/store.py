"""Catalog storage for quackpack.

Persists queries in a single human-readable, diffable YAML file so the library
is portable and git-friendly. This module owns the catalog file end to end: the
:class:`Query` record, load/save, and CRUD + a small substring search.

Storage location
----------------
The catalog lives at ``$QUACKPACK_HOME/pack.yaml`` (default ``~/.quackpack``).
Set ``QUACKPACK_HOME`` to relocate it — handy for tests and for keeping a pack
inside a git repo.

File shape
----------
.. code-block:: yaml

    version: 1
    queries:
      - name: top-customers
        sql: select * from read_csv_auto(:file) order by spend desc limit :n
        tags: [sales, adhoc]
        desc: Biggest spenders in a CSV.
        created: "2026-06-22T19:40:00+00:00"
        params: [file, n]
        presets:
          q3-2026: {n: 25}
        run_count: 3
        last_run: "2026-06-25T19:40:00+00:00"
        last_status: ok
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

import yaml

from .history import OK, now_iso
from .params import extract_params

__all__ = [
    "Query",
    "Catalog",
    "CatalogError",
    "DuplicateQueryError",
    "QueryNotFoundError",
    "PresetError",
    "PresetNotFoundError",
    "catalog_home",
    "catalog_path",
]

CATALOG_VERSION = 1


class CatalogError(Exception):
    """Base class for catalog problems."""


class DuplicateQueryError(CatalogError):
    """Raised when adding a query whose name already exists."""


class QueryNotFoundError(CatalogError):
    """Raised when a named query can't be found."""


class PresetError(CatalogError):
    """Base class for problems manipulating a query's param presets."""


class PresetNotFoundError(PresetError):
    """Raised when a named preset can't be found on a query."""


def catalog_home() -> Path:
    """Return the quackpack home dir (``$QUACKPACK_HOME`` or ``~/.quackpack``)."""
    override = os.environ.get("QUACKPACK_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".quackpack"


def catalog_path() -> Path:
    """Return the full path to the catalog YAML file."""
    return catalog_home() / "pack.yaml"


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string (seconds precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _coerce_int(value: object) -> int:
    """Best-effort int from a loaded YAML field (tolerant of bad hand edits)."""
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _clean_binding(values: object) -> dict[str, Any]:
    """Normalise one preset's ``{param: value}`` mapping.

    Coerces to a plain dict with stripped, non-empty string keys. Non-mapping
    input (a garbled hand edit) degrades to an empty binding rather than raising
    so a single bad preset can't make the whole catalog unloadable.
    """
    if not isinstance(values, dict):
        return {}
    out: dict[str, Any] = {}
    for key, val in values.items():
        k = str(key).strip()
        if k:
            out[k] = val
    return out


def _clean_presets(presets: object) -> dict[str, dict[str, Any]]:
    """Normalise the whole ``presets`` mapping (name -> binding).

    Strips/drops blank preset names and cleans each binding via
    :func:`_clean_binding`. Tolerant of a non-mapping value on load.
    """
    if not isinstance(presets, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, binding in presets.items():
        key = str(name).strip()
        if key:
            out[key] = _clean_binding(binding)
    return out


@dataclass
class Query:
    """A single saved query and its metadata.

    ``params`` is derived from the SQL when omitted, so callers normally only
    supply ``name`` and ``sql``.
    """

    name: str
    sql: str
    tags: list[str] = field(default_factory=list)
    desc: str = ""
    created: str = field(default_factory=_now_iso)
    params: list[str] = field(default_factory=list)
    presets: dict[str, dict[str, Any]] = field(default_factory=dict)
    run_count: int = 0
    last_run: str = ""
    last_status: str = ""

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        self.sql = self.sql.strip()
        # Normalise tags: strip, drop blanks, de-dupe while preserving order.
        seen: dict[str, None] = {}
        for tag in self.tags:
            t = str(tag).strip()
            if t:
                seen.setdefault(t, None)
        self.tags = list(seen)
        self.desc = (self.desc or "").strip()
        if not self.params:
            self.params = extract_params(self.sql)
        # Presets: a mapping of preset-name -> {param: value}. Normalise the
        # names (strip/drop blanks) and coerce each binding set to a plain dict
        # with string keys, tolerating garbled hand edits on load.
        self.presets = _clean_presets(self.presets)
        # History fields are tolerant of absent/garbled values on load.
        self.run_count = _coerce_int(self.run_count)
        self.last_run = (self.last_run or "").strip()
        self.last_status = (self.last_status or "").strip()

    def record_run(self, status: str = OK, *, when: Optional[str] = None) -> None:
        """Mutate this query in place to reflect one more run with *status*.

        Bumps :attr:`run_count` and refreshes :attr:`last_run` /
        :attr:`last_status`. Persisting is the caller's job (see
        :meth:`Catalog.record_run`).
        """
        self.run_count += 1
        self.last_run = when or now_iso()
        self.last_status = status

    # -- presets -----------------------------------------------------------

    def set_preset(self, name: str, values: dict[str, Any]) -> dict[str, Any]:
        """Create or replace the preset *name* with *values*, returning it.

        *values* is a ``{param: value}`` mapping. Keys are stripped; empty keys
        are dropped. Adding a preset never validates that the params exist on
        the query (a query's ``:param`` set can change), but the CLI warns when
        a preset references an unknown one.
        """
        key = name.strip()
        if not key:
            raise PresetError("Preset name must not be empty.")
        self.presets[key] = _clean_binding(values)
        return self.presets[key]

    def get_preset(self, name: str) -> dict[str, Any]:
        """Return the binding set for preset *name* or raise."""
        key = name.strip()
        try:
            return self.presets[key]
        except KeyError:
            raise PresetNotFoundError(
                f"Query {self.name!r} has no preset named {name!r}."
            ) from None

    def remove_preset(self, name: str) -> dict[str, Any]:
        """Remove and return the binding set for preset *name* or raise."""
        key = name.strip()
        try:
            return self.presets.pop(key)
        except KeyError:
            raise PresetNotFoundError(
                f"Query {self.name!r} has no preset named {name!r}."
            ) from None

    def to_dict(self) -> dict:
        """Serialise to a plain dict suitable for YAML dumping."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Query":
        """Build a :class:`Query` from a loaded YAML mapping (tolerant of extras)."""
        return cls(
            name=data.get("name", ""),
            sql=data.get("sql", ""),
            tags=list(data.get("tags") or []),
            desc=data.get("desc", "") or "",
            created=data.get("created") or _now_iso(),
            params=list(data.get("params") or []),
            # Pass the raw presets value through; __post_init__ normalises and
            # tolerates non-mapping junk via _clean_presets.
            presets=data.get("presets") or {},
            run_count=data.get("run_count", 0),
            last_run=data.get("last_run", "") or "",
            last_status=data.get("last_status", "") or "",
        )


class Catalog:
    """A YAML-backed collection of :class:`Query` records.

    Use :meth:`load` to read the on-disk catalog. Mutating methods
    (:meth:`add`, :meth:`remove`) persist immediately so the file always
    reflects the latest state ("survives restart").
    """

    def __init__(self, path: Optional[Path] = None, queries: Optional[Iterable[Query]] = None) -> None:
        self.path = Path(path) if path is not None else catalog_path()
        self._queries: list[Query] = list(queries or [])

    # -- loading / saving --------------------------------------------------

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Catalog":
        """Load the catalog from disk, returning an empty one if absent."""
        p = Path(path) if path is not None else catalog_path()
        if not p.exists():
            return cls(path=p)
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise CatalogError(f"Malformed catalog at {p}: expected a mapping.")
        items = raw.get("queries") or []
        if not isinstance(items, list):
            raise CatalogError(f"Malformed catalog at {p}: 'queries' must be a list.")
        queries = [Query.from_dict(item) for item in items if isinstance(item, dict)]
        return cls(path=p, queries=queries)

    def save(self) -> None:
        """Atomically write the catalog to :attr:`path` (creating parents)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CATALOG_VERSION,
            "queries": [q.to_dict() for q in self._queries],
        }
        text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100)
        # Write to a temp file in the same dir, then replace, so a crash never
        # leaves a half-written catalog.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path)

    # -- read --------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._queries)

    def __iter__(self) -> Iterator[Query]:
        return iter(self._queries)

    def names(self) -> list[str]:
        """All query names, in stored order."""
        return [q.name for q in self._queries]

    def get(self, name: str) -> Query:
        """Return the query named *name* or raise :class:`QueryNotFoundError`."""
        key = name.strip()
        for q in self._queries:
            if q.name == key:
                return q
        raise QueryNotFoundError(f"No query named {name!r}.")

    def list(self, tag: Optional[str] = None) -> list[Query]:
        """Return queries, optionally filtered to those carrying *tag*.

        Results are sorted by name for stable, scannable output.
        """
        items = list(self._queries)
        if tag:
            t = tag.strip()
            items = [q for q in items if t in q.tags]
        return sorted(items, key=lambda q: q.name.lower())

    def search(self, text: str) -> list[Query]:
        """Substring match over name, sql, desc, and tags (case-insensitive)."""
        needle = text.strip().lower()
        if not needle:
            return self.list()
        hits = [
            q
            for q in self._queries
            if needle in q.name.lower()
            or needle in q.sql.lower()
            or needle in q.desc.lower()
            or any(needle in tag.lower() for tag in q.tags)
        ]
        return sorted(hits, key=lambda q: q.name.lower())

    # -- write -------------------------------------------------------------

    def add(self, query: Query, *, overwrite: bool = False, save: bool = True) -> Query:
        """Add *query*; raise on duplicate name unless *overwrite* is set."""
        if not query.name:
            raise CatalogError("Query name must not be empty.")
        if not query.sql:
            raise CatalogError("Query SQL must not be empty.")
        existing = next((i for i, q in enumerate(self._queries) if q.name == query.name), None)
        if existing is not None:
            if not overwrite:
                raise DuplicateQueryError(
                    f"A query named {query.name!r} already exists (use overwrite to replace)."
                )
            self._queries[existing] = query
        else:
            self._queries.append(query)
        if save:
            self.save()
        return query

    def remove(self, name: str, *, save: bool = True) -> Query:
        """Remove and return the query named *name*."""
        key = name.strip()
        for i, q in enumerate(self._queries):
            if q.name == key:
                removed = self._queries.pop(i)
                if save:
                    self.save()
                return removed
        raise QueryNotFoundError(f"No query named {name!r}.")

    def update(self, query: Query, *, save: bool = True) -> Query:
        """Replace an existing query (matched by name) in place.

        Unlike :meth:`add`, this requires the name to already exist (it's the
        backing operation for ``edit``). Position in the catalog is preserved so
        an edit never reshuffles the file.
        """
        if not query.name:
            raise CatalogError("Query name must not be empty.")
        if not query.sql:
            raise CatalogError("Query SQL must not be empty.")
        for i, q in enumerate(self._queries):
            if q.name == query.name:
                self._queries[i] = query
                if save:
                    self.save()
                return query
        raise QueryNotFoundError(f"No query named {query.name!r}.")

    def record_run(self, name: str, status: str = OK, *, save: bool = True) -> Query:
        """Bump run history for the query named *name* and persist it.

        Records one execution (incrementing ``run_count`` and refreshing
        ``last_run`` / ``last_status``). Called by the ``run`` command after an
        execution completes — successfully or not — so ``ls`` can show recency
        and the last outcome. Raises :class:`QueryNotFoundError` if the query
        vanished between fetch and record.
        """
        q = self.get(name)
        q.record_run(status)
        if save:
            self.save()
        return q

    # -- presets -----------------------------------------------------------

    def set_preset(
        self, query_name: str, preset: str, values: dict, *, save: bool = True
    ) -> Query:
        """Create/replace *preset* on the named query and persist.

        Returns the owning :class:`Query`. Raises :class:`QueryNotFoundError`
        if the query is unknown or :class:`PresetError` if the preset name is
        empty.
        """
        q = self.get(query_name)
        q.set_preset(preset, values)
        if save:
            self.save()
        return q

    def remove_preset(
        self, query_name: str, preset: str, *, save: bool = True
    ) -> Query:
        """Remove *preset* from the named query and persist.

        Raises :class:`QueryNotFoundError` if the query is unknown or
        :class:`PresetNotFoundError` if it has no such preset.
        """
        q = self.get(query_name)
        q.remove_preset(preset)
        if save:
            self.save()
        return q
