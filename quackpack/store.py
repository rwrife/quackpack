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
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

import yaml

from .params import extract_params

__all__ = [
    "Query",
    "Catalog",
    "CatalogError",
    "DuplicateQueryError",
    "QueryNotFoundError",
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
