"""Export / import for quackpack (backlog #5).

quackpack's ``pack.yaml`` is already a single portable file, but there is no
first-class way to hand a teammate a *curated subset* of your queries or to
*merge* someone else's pack into yours without hand-editing YAML and risking a
clobber. This module closes that loop:

* :func:`build_export` turns a selection of :class:`~quackpack.store.Query`
  records into a standalone, importable pack document — **queries + presets and
  their metadata only**. Run history and result snapshots are deliberately
  excluded: those are *local* state, not something you share.
* :func:`plan_import` merges an exported document into an existing catalog under
  one of three collision strategies (``skip`` / ``overwrite`` / ``rename``),
  never silently clobbering by default, and reports an
  ``imported / skipped / renamed`` summary.

Both halves are pure functions over plain data (a catalog is only ever read via
its public ``__iter__`` / ``get`` / ``names``), so the sharing logic is trivial
to unit-test and the CLI layer just does I/O around it.

Export document shape
---------------------
The same top-level shape as ``pack.yaml`` (so an export *is* a pack you could
point ``QUACKPACK_HOME`` at), but every query is stripped down to the sharable
fields::

    version: 1
    exported: "2026-07-09T19:40:00+00:00"
    queries:
      - name: top-customers
        sql: select * from read_csv_auto(:file) order by spend desc limit :n
        tags: [sales]
        desc: Biggest spenders in a CSV.
        created: "2026-06-22T19:40:00+00:00"
        params: [file, n]
        presets:
          q3-2026: {n: 25}

``run_count`` / ``last_run`` / ``last_status`` are omitted on export; snapshots
never touch this file at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .store import CATALOG_VERSION, Query
from .templating import extract_refs

__all__ = [
    "EXPORT_VERSION",
    "IMPORT_STRATEGIES",
    "ImportError_",
    "ExportSelection",
    "ImportPlan",
    "sharable_dict",
    "select_queries",
    "dangling_refs",
    "build_export",
    "parse_export",
    "plan_import",
    "missing_names",
]

# The exported document uses the same schema version as the on-disk catalog so
# an export can be consumed as a pack directly; bumping either bumps both.
EXPORT_VERSION = CATALOG_VERSION

# Collision strategies for :func:`plan_import`. ``skip`` is the safe default:
# an incoming query whose name already exists is left untouched.
IMPORT_STRATEGIES = ("skip", "overwrite", "rename")

# Fields that make a query *sharable*. Anything outside this set (run history)
# is intentionally dropped on export.
_SHARABLE_FIELDS = ("name", "sql", "tags", "desc", "created", "params", "presets")

# A rename suffix looks like ``name-2`` / ``name-3`` … We start collisions at
# ``-2`` (the original is implicitly ``-1``), matching how humans dedupe files.
_RENAME_SUFFIX_RE = re.compile(r"^(?P<stem>.*)-(?P<n>\d+)$")


class ImportError_(Exception):
    """A malformed / unreadable export document supplied to ``import``.

    Named with a trailing underscore so it never shadows the builtin
    :class:`ImportError`; the CLI maps it to the exit-code ``1`` "bad file" case.
    """


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string (seconds precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sharable_dict(query: Query) -> dict[str, Any]:
    """Serialise *query* to just its sharable fields (drop run history).

    The result round-trips through :meth:`Query.from_dict`, reproducing the
    query and its presets exactly while omitting ``run_count`` / ``last_run`` /
    ``last_status``. Snapshots are stored elsewhere and never appear here.
    """
    full = query.to_dict()
    return {key: full[key] for key in _SHARABLE_FIELDS if key in full}


def select_queries(
    queries: Iterable[Query],
    names: Optional[Iterable[str]] = None,
    *,
    tag: Optional[str] = None,
) -> list[Query]:
    """Filter *queries* down to an export selection (name args + optional tag).

    Selection rules, combined with AND when both are given:

    * *names* — keep only queries whose name is in this set. Unknown names are
      the caller's problem to report (see :func:`missing_names`); this function
      just intersects.
    * *tag* — keep only queries carrying *tag*.

    With neither filter the whole input is returned. Output preserves the input
    order so an export is stable/diffable.
    """
    want = {n.strip() for n in names if n and n.strip()} if names else None
    t = tag.strip() if tag else None
    out: list[Query] = []
    for q in queries:
        if want is not None and q.name not in want:
            continue
        if t is not None and t not in q.tags:
            continue
        out.append(q)
    return out


def missing_names(
    queries: Iterable[Query], names: Iterable[str]
) -> list[str]:
    """Return the requested *names* that don't exist in *queries*.

    Lets the CLI fail cleanly on ``export typo-name`` instead of silently
    exporting nothing. Order follows *names* (de-duplicated).
    """
    have = {q.name for q in queries}
    seen: dict[str, None] = {}
    for n in names:
        key = (n or "").strip()
        if key and key not in have:
            seen.setdefault(key, None)
    return list(seen)


def dangling_refs(selected: Iterable[Query]) -> dict[str, list[str]]:
    """Map each selected query to any ``{{ ref }}`` it makes *outside* the selection.

    Templating references survive a round trip only if the referenced query is
    also exported; a reference to a query you're *not* exporting would break on
    import. This detects those so :func:`build_export` / the CLI can warn (the
    export is still produced — it's a heads-up, not a hard error).

    Returns ``{query_name: [missing_ref, ...]}`` for queries that have at least
    one dangling ref; queries whose refs are all in-selection are omitted.
    """
    selected = list(selected)
    in_selection = {q.name for q in selected}
    out: dict[str, list[str]] = {}
    for q in selected:
        missing = [ref for ref in extract_refs(q.sql) if ref not in in_selection]
        if missing:
            out[q.name] = missing
    return out


@dataclass
class ExportSelection:
    """The outcome of preparing an export: the document plus any ref warnings.

    * :attr:`document` — the ready-to-serialise mapping (see module docstring).
    * :attr:`queries` — the selected :class:`Query` records, in export order.
    * :attr:`dangling` — ``{name: [missing_ref, ...]}`` for queries referencing
      a query left out of the selection (empty when the selection is closed).
    """

    document: dict[str, Any]
    queries: list[Query]
    dangling: dict[str, list[str]] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.queries)


def build_export(
    queries: Iterable[Query],
    names: Optional[Iterable[str]] = None,
    *,
    tag: Optional[str] = None,
) -> ExportSelection:
    """Build an :class:`ExportSelection` from *queries* under the given filters.

    Applies :func:`select_queries` (name args and/or ``tag``), serialises each
    survivor via :func:`sharable_dict`, and computes :func:`dangling_refs` so
    the caller can warn about templating references that would break on import.
    An empty selection still yields a valid (empty) document — exporting nothing
    is success, not an error (exit code 0).
    """
    selected = select_queries(queries, names, tag=tag)
    document = {
        "version": EXPORT_VERSION,
        "exported": _now_iso(),
        "queries": [sharable_dict(q) for q in selected],
    }
    return ExportSelection(
        document=document,
        queries=selected,
        dangling=dangling_refs(selected),
    )


def parse_export(raw: Any, *, source: str = "import file") -> list[Query]:
    """Validate a loaded export document and return its :class:`Query` records.

    *raw* is the already-parsed YAML/JSON (a mapping). Raises
    :class:`ImportError_` — mapped by the CLI to exit code ``1`` — when the
    document isn't a mapping, lacks a list ``queries``, or contains an entry
    that can't be read as a query (missing/blank ``name`` or ``sql``). Extra
    top-level keys are ignored so a full ``pack.yaml`` can be imported as-is.
    """
    if not isinstance(raw, dict):
        raise ImportError_(f"Malformed {source}: expected a mapping at the top level.")
    items = raw.get("queries")
    if items is None:
        raise ImportError_(f"Malformed {source}: no 'queries' key.")
    if not isinstance(items, list):
        raise ImportError_(f"Malformed {source}: 'queries' must be a list.")

    out: list[Query] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise ImportError_(
                f"Malformed {source}: query #{idx + 1} is not a mapping."
            )
        query = Query.from_dict(item)
        if not query.name:
            raise ImportError_(
                f"Malformed {source}: query #{idx + 1} has no name."
            )
        if not query.sql:
            raise ImportError_(
                f"Malformed {source}: query {query.name!r} has empty SQL."
            )
        # Re-materialise through the sharable projection so run history never
        # rides in from the source (importing a raw ``pack.yaml`` shouldn't
        # graft someone else's run counts onto your queries).
        out.append(Query.from_dict(sharable_dict(query)))
    return out


def _rename_collision(name: str, taken: set[str]) -> str:
    """Return the first free ``name-2`` / ``name-3`` … not already in *taken*.

    If *name* already ends in a ``-<n>`` suffix we bump that number rather than
    stacking suffixes, so re-importing a renamed query stays tidy
    (``report-2`` -> ``report-3``, not ``report-2-2``).
    """
    m = _RENAME_SUFFIX_RE.match(name)
    if m:
        stem = m.group("stem")
        start = int(m.group("n")) + 1
    else:
        stem = name
        start = 2
    n = start
    while f"{stem}-{n}" in taken:
        n += 1
    return f"{stem}-{n}"


def _stamped(query: Query, extra_tag: Optional[str], *, name: Optional[str] = None) -> Query:
    """Return a fresh :class:`Query` copy, optionally renamed and provenance-tagged.

    Builds from the sharable dict (so history never leaks in) and appends
    *extra_tag* if given; :class:`Query`'s own normalisation de-dupes tags, so
    stamping a tag the query already carries is a no-op.
    """
    data = sharable_dict(query)
    if name is not None:
        data["name"] = name
    if extra_tag:
        tags = list(data.get("tags") or [])
        tags.append(extra_tag)
        data["tags"] = tags
    return Query.from_dict(data)


@dataclass
class ImportPlan:
    """A resolved (but not yet applied) import merge.

    * :attr:`to_add` — queries to write into the catalog (already renamed and
      provenance-tagged as needed). ``overwrite`` collisions appear here too.
    * :attr:`overwrite` — names among :attr:`to_add` that replace an existing
      query (a subset used only for reporting / to drive ``add(overwrite=True)``).
    * :attr:`skipped` — incoming names left untouched (``skip`` strategy, name
      collision).
    * :attr:`renamed` — ``{original_name: new_name}`` for ``rename`` collisions.
    """

    to_add: list[Query] = field(default_factory=list)
    overwrite: set[str] = field(default_factory=set)
    skipped: list[str] = field(default_factory=list)
    renamed: dict[str, str] = field(default_factory=dict)

    @property
    def imported_count(self) -> int:
        return len(self.to_add)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    @property
    def renamed_count(self) -> int:
        return len(self.renamed)

    def summary(self) -> str:
        """One-line ``imported N, skipped M, renamed K`` recap for the CLI."""
        return (
            f"imported {self.imported_count}, "
            f"skipped {self.skipped_count}, "
            f"renamed {self.renamed_count}"
        )


def plan_import(
    existing_names: Iterable[str],
    incoming: Iterable[Query],
    *,
    strategy: str = "skip",
    tag: Optional[str] = None,
) -> ImportPlan:
    """Plan how *incoming* queries merge into a catalog holding *existing_names*.

    Pure planning step: it computes what *would* happen without mutating
    anything, so the CLI can apply the plan and report deterministically, and so
    the whole merge is unit-testable without touching disk.

    Strategy on a **name collision** (an incoming name already in the catalog):

    * ``skip`` (default) — leave the existing query alone; record the incoming
      name under :attr:`~ImportPlan.skipped`. Never clobbers.
    * ``overwrite`` — replace the existing query with the incoming one.
    * ``rename`` — import the incoming query under a suffixed name
      (``name-2``…), recorded in :attr:`~ImportPlan.renamed`.

    Non-colliding queries are always added. Within a single import, names
    claimed by earlier renames are reserved so two incoming collisions can't
    rename to the same target. *tag*, when given, is appended to every imported
    query for provenance (e.g. ``from-alice``).

    Raises :class:`ValueError` for an unknown *strategy* (a CLI usage error).
    """
    if strategy not in IMPORT_STRATEGIES:
        raise ValueError(
            f"Unknown import strategy {strategy!r}; "
            f"choose one of: {', '.join(IMPORT_STRATEGIES)}."
        )

    # Track every name that will exist post-import (start from the catalog) so
    # rename never targets an occupied slot — existing, or freshly imported.
    taken: set[str] = {n.strip() for n in existing_names if n and n.strip()}
    original: set[str] = set(taken)
    plan = ImportPlan()

    for q in incoming:
        name = q.name
        collides = name in original
        if not collides:
            plan.to_add.append(_stamped(q, tag))
            taken.add(name)
            continue

        if strategy == "skip":
            plan.skipped.append(name)
        elif strategy == "overwrite":
            plan.to_add.append(_stamped(q, tag))
            plan.overwrite.add(name)
            # name already in taken/original; overwriting keeps it occupied.
        else:  # rename
            new_name = _rename_collision(name, taken)
            plan.to_add.append(_stamped(q, tag, name=new_name))
            plan.renamed[name] = new_name
            taken.add(new_name)

    return plan
