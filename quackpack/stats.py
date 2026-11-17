"""Pure usage summaries and conservative cleanup plans for a saved pack."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .history import OK, parse_iso
from .store import Query
from .templating import extract_refs


@dataclass
class QueryUsage:
    """The metadata needed to inspect usage, without SQL or preset values."""

    name: str
    run_count: int
    last_run: str
    last_status: str
    created: str

    @classmethod
    def from_query(cls, query: Query) -> QueryUsage:
        return cls(
            query.name, query.run_count, query.last_run, query.last_status, query.created
        )


@dataclass
class PackStats:
    total_queries: int
    total_runs: int
    never_run_count: int
    most_run: list[QueryUsage]
    oldest_untouched: list[QueryUsage]
    tag_distribution: dict[str, int]
    last_errors: list[QueryUsage]


def _never_run(query: Query) -> bool:
    return query.run_count == 0 and not query.last_run


def summarize(
    queries: Iterable[Query], *, top: int = 5, tag: str | None = None
) -> PackStats:
    """Summarize inline history, scoped to an exact tag if supplied.

    Oldest-untouched uses last run, or creation for never-run queries. Unknown
    dates are omitted from that ranking. Ties are broken by query name.
    """
    if top < 1:
        raise ValueError("top must be at least 1")
    rows = [q for q in queries if tag is None or tag.strip() in q.tags]
    rows.sort(key=lambda q: q.name)
    most_run = sorted(
        (q for q in rows if q.run_count > 0),
        key=lambda q: (-q.run_count, q.name),
    )[:top]
    dated = [
        (q, parse_iso(q.created if _never_run(q) else q.last_run)) for q in rows
    ]
    oldest = sorted(
        ((q, when) for q, when in dated if when is not None),
        key=lambda item: (item[1], item[0].name),
    )[:top]
    tags = Counter(tag for q in rows for tag in q.tags)
    return PackStats(
        total_queries=len(rows),
        total_runs=sum(q.run_count for q in rows),
        never_run_count=sum(_never_run(q) for q in rows),
        most_run=[QueryUsage.from_query(q) for q in most_run],
        oldest_untouched=[QueryUsage.from_query(q) for q, _ in oldest],
        tag_distribution=dict(sorted(tags.items())),
        last_errors=[
            QueryUsage.from_query(q)
            for q in rows
            if q.last_status and q.last_status != OK
        ],
    )


@dataclass
class PrunePlan:
    candidates: list[Query]
    protected: dict[str, str]
    unknown_history: list[str]


def plan_prune(
    queries: Iterable[Query], *, older_than: int, now: datetime
) -> PrunePlan:
    """Select never-run OR strictly older-than-days queries, without mutation.

    Presets and incoming references protect queries even if their dependents
    are also candidates. Unknown run dates are reported, never guessed stale.
    The caller supplies the clock so planning is deterministic and pure.
    """
    if older_than < 0:
        raise ValueError("older_than must not be negative")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    rows = sorted(queries, key=lambda q: q.name)
    referenced = {
        ref for q in rows for ref in extract_refs(q.sql) if ref != q.name
    }
    cutoff = now - timedelta(days=older_than)
    candidates: list[Query] = []
    protected: dict[str, str] = {}
    unknown_history: list[str] = []
    for query in rows:
        reasons = []
        if query.presets:
            reasons.append("has presets")
        if query.name in referenced:
            reasons.append("referenced by another query")
        if reasons:
            protected[query.name] = "; ".join(reasons)
            continue
        last_run = parse_iso(query.last_run)
        if _never_run(query) or (last_run is not None and last_run < cutoff):
            candidates.append(query)
        elif last_run is None:
            unknown_history.append(query.name)
    return PrunePlan(candidates, protected, unknown_history)
