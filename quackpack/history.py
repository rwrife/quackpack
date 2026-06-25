"""Run history for quackpack queries.

Every ``quackpack run`` bumps a small per-query log so the catalog can answer
"when did I last use this, and did it work?" without a separate database. The
history lives *on the* :class:`~quackpack.store.Query` record (persisted in the
same YAML catalog) as three fields:

* ``run_count`` — how many times the query has been executed.
* ``last_run`` — ISO-8601 UTC timestamp of the most recent run (``""`` if never).
* ``last_status`` — ``"ok"`` or ``"error"`` for that most recent run (``""`` if
  never).

This module is intentionally tiny and pure: it owns the timestamp format and the
"N ago" humanisation, so both the store (which records runs) and the CLI (which
renders "last run 3d ago") share one implementation. The actual mutation of a
``Query`` is a one-liner in the store; keeping the helpers here means they are
trivial to unit-test in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

__all__ = [
    "RunHistory",
    "OK",
    "ERROR",
    "now_iso",
    "parse_iso",
    "humanize_age",
    "describe_last_run",
]

OK = "ok"
ERROR = "error"


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string (seconds precision).

    Mirrors :func:`quackpack.store._now_iso` so ``created`` and ``last_run``
    timestamps share an identical, comparable format.
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, returning ``None`` if it's blank/garbage.

    Naive timestamps (no tz) are assumed to be UTC so age math never raises on
    a hand-edited catalog.

    >>> parse_iso("") is None
    True
    >>> parse_iso("not-a-date") is None
    True
    >>> parse_iso("2026-06-25T19:40:00+00:00").year
    2026
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def humanize_age(then: str, *, now: Optional[datetime] = None) -> str:
    """Return a compact "time since *then*" label like ``3d`` or ``just now``.

    *then* is an ISO-8601 string (typically a query's ``last_run``). An empty or
    unparseable value yields ``"never"``. Units step seconds → minutes → hours →
    days → weeks → years, picking the largest whole unit so output stays short
    enough for an ``ls`` column. Future timestamps (clock skew / hand edits)
    clamp to ``"just now"`` rather than printing a negative age.

    >>> humanize_age("")
    'never'
    >>> import datetime as _dt
    >>> base = _dt.datetime(2026, 6, 25, 12, 0, tzinfo=_dt.timezone.utc)
    >>> humanize_age("2026-06-25T11:59:30+00:00", now=base)
    'just now'
    >>> humanize_age("2026-06-25T11:00:00+00:00", now=base)
    '1h ago'
    >>> humanize_age("2026-06-23T12:00:00+00:00", now=base)
    '2d ago'
    >>> humanize_age("2026-06-04T12:00:00+00:00", now=base)
    '3w ago'
    """
    dt = parse_iso(then)
    if dt is None:
        return "never"
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:  # pragma: no cover - defensive
        current = current.replace(tzinfo=timezone.utc)

    seconds = (current - dt).total_seconds()
    if seconds < 45:
        # Covers tiny positives and any future timestamp (negative seconds).
        return "just now"

    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 7:
        return f"{int(days)}d ago"
    weeks = days / 7
    if weeks < 52:
        return f"{int(weeks)}w ago"
    years = days / 365
    return f"{int(years)}y ago"


def describe_last_run(last_run: str, last_status: str, *, now: Optional[datetime] = None) -> str:
    """Human label combining recency and outcome, e.g. ``3d ago`` / ``2h ago (error)``.

    Used by ``ls`` so a glance shows both *when* a query last ran and whether it
    blew up. A never-run query is just ``"never"`` (no status noise).

    >>> describe_last_run("", "")
    'never'
    >>> import datetime as _dt
    >>> base = _dt.datetime(2026, 6, 25, 12, 0, tzinfo=_dt.timezone.utc)
    >>> describe_last_run("2026-06-25T10:00:00+00:00", "ok", now=base)
    '2h ago'
    >>> describe_last_run("2026-06-25T10:00:00+00:00", "error", now=base)
    '2h ago (error)'
    """
    age = humanize_age(last_run, now=now)
    if age == "never":
        return age
    if last_status and last_status != OK:
        return f"{age} ({last_status})"
    return age


@dataclass
class RunHistory:
    """Plain holder for a query's run history fields.

    The :class:`~quackpack.store.Query` stores these inline rather than nesting a
    ``RunHistory`` object (keeps the YAML flat and diff-friendly), but this
    dataclass is handy for passing the trio around and for tests.
    """

    run_count: int = 0
    last_run: str = ""
    last_status: str = ""

    def record(self, status: str, *, when: Optional[str] = None) -> "RunHistory":
        """Return a new history reflecting one more run with *status*."""
        return RunHistory(
            run_count=self.run_count + 1,
            last_run=when or now_iso(),
            last_status=status,
        )
