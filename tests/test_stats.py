"""Pure usage aggregation and prune safety."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from quackpack.stats import plan_prune, summarize
from quackpack.store import Query

NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)


def test_summary_rankings_tags_errors_and_no_mutation() -> None:
    queries = [
        Query("zebra", "select 1", tags=["sales"], run_count=8,
              last_run="2026-11-16T00:00:00Z", last_status="ok"),
        Query("alpha", "select 1", tags=["sales", "adhoc"], run_count=8,
              last_run="2025-01-01T00:00:00Z", last_status="error"),
        Query("never", "select 1", tags=["adhoc"], created="2024-01-01T00:00:00Z"),
        Query("unknown", "select 1", run_count=2, last_run="garbled", last_status="cancelled"),
    ]
    before = deepcopy(queries)
    stats = summarize(queries, top=2)
    assert (stats.total_queries, stats.total_runs, stats.never_run_count) == (4, 18, 1)
    assert [q.name for q in stats.most_run] == ["alpha", "zebra"]
    assert [q.name for q in stats.oldest_untouched] == ["never", "alpha"]
    assert stats.tag_distribution == {"adhoc": 2, "sales": 2}
    assert [q.name for q in stats.last_errors] == ["alpha", "unknown"]
    assert queries == before

    scoped = summarize(queries, top=1, tag=" sales ")
    assert (scoped.total_queries, scoped.total_runs, scoped.never_run_count) == (2, 16, 0)
    assert scoped.tag_distribution == {"adhoc": 1, "sales": 2}
    assert [q.name for q in scoped.most_run] == ["alpha"]
    assert [q.name for q in scoped.oldest_untouched] == ["alpha"]
    assert [q.name for q in scoped.last_errors] == ["alpha"]
    assert summarize(queries, tag="SALES") == summarize([])


def test_empty_summary_and_unknown_dates() -> None:
    stats = summarize([])
    assert (stats.total_queries, stats.total_runs, stats.never_run_count) == (0, 0, 0)
    assert stats.most_run == stats.oldest_untouched == stats.last_errors == []
    assert stats.tag_distribution == {}
    assert summarize([Query("unknown", "select 1", created="bad")]).oldest_untouched == []
    with pytest.raises(ValueError, match="top"):
        summarize([], top=0)


def test_prune_never_run_stale_boundary_and_invalid_history() -> None:
    cutoff = NOW - timedelta(days=180)
    queries = [
        Query("never-new", "select 1", created=NOW.isoformat()),
        Query("stale", "select 1", run_count=1,
              last_run=(cutoff - timedelta(seconds=1)).isoformat()),
        Query("boundary", "select 1", run_count=1, last_run=cutoff.isoformat()),
        Query("recent", "select 1", run_count=1, last_run=NOW.isoformat()),
        Query("future", "select 1", run_count=1, last_run=(NOW + timedelta(days=1)).isoformat()),
        Query("invalid", "select 1", run_count=1, last_run="bad"),
        Query("missing", "select 1", run_count=1),
        Query("date-only", "select 1", last_run="2020-01-01"),
    ]
    before = deepcopy(queries)
    plan = plan_prune(queries, older_than=180, now=NOW)
    assert [q.name for q in plan.candidates] == ["date-only", "never-new", "stale"]
    assert plan.unknown_history == ["invalid", "missing"]
    assert queries == before
    assert "boundary" in [
        q.name for q in plan_prune(queries, older_than=179, now=NOW).candidates
    ]
    assert plan_prune([], older_than=0, now=NOW).candidates == []
    with pytest.raises(ValueError, match="older_than"):
        plan_prune([], older_than=-1, now=NOW)


def test_prune_presets_and_transitive_reference_guards() -> None:
    queries = [
        Query("preset", "select :n", presets={"empty": {}}),
        Query("base", "select 1"),
        Query("middle", "select * from {{ base }}"),
        Query("outer", "select * from {{ middle }}"),
        Query("literal", "select '{{ base }}'"),
        Query("cycle-a", "select * from {{ cycle-b }}"),
        Query("cycle-b", "select * from {{ cycle-a }}"),
    ]
    plan = plan_prune(queries, older_than=180, now=NOW)
    assert [q.name for q in plan.candidates] == ["literal", "outer"]
    assert set(plan.protected) == {"preset", "base", "middle", "cycle-a", "cycle-b"}
    assert plan.protected["preset"] == "has presets"
    assert plan.protected["base"] == "referenced by another query"
    assert [q.name for q in plan_prune(
        [Query("base", "select 1"), Query("literal", "select '{{ base }}'")],
        older_than=180, now=NOW,
    ).candidates] == ["base", "literal"]
