"""Stats output and persisted prune behavior through the real CLI."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quackpack.cli import app
from quackpack.store import Catalog, Query

runner = CliRunner()


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUACKPACK_HOME", str(tmp_path))


def test_stats_json_and_rich_display() -> None:
    catalog = Catalog()
    catalog.add(Query("used", "select 1", tags=["sales", "demo"], run_count=3,
                      last_run="2020-01-01T00:00:00Z", last_status="error"))
    catalog.add(Query("unused", "select 2", tags=["demo"], created="2019-01-01"))
    result = runner.invoke(app, ["stats", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["total_queries"] == 2
    assert summary["total_runs"] == 3
    assert summary["never_run_count"] == 1
    assert summary["most_run"][0]["name"] == "used"
    assert summary["oldest_untouched"][0]["name"] == "unused"
    assert summary["last_errors"][0]["last_status"] == "error"
    assert summary["tag_distribution"] == {"demo": 2, "sales": 1}
    assert "sql" not in summary["most_run"][0]

    scoped = runner.invoke(app, ["stats", "--format", "json", "--tag", "sales", "--top", "1"])
    assert scoped.exit_code == 0, scoped.stderr
    data = json.loads(scoped.stdout)
    assert (data["total_queries"], data["never_run_count"]) == (1, 0)
    assert len(data["oldest_untouched"]) == 1

    display = runner.invoke(app, ["stats"])
    assert display.exit_code == 0, display.stderr
    for text in ("Total queries", "Total runs", "Never run", "Most-run",
                 "Oldest-untouched", "Tag distribution", "Last-error queries", "used"):
        assert text in display.stdout


def test_empty_stats_json() -> None:
    result = runner.invoke(app, ["stats", "--format", "json", "--tag", "missing"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {
        "total_queries": 0, "total_runs": 0, "never_run_count": 0,
        "most_run": [], "oldest_untouched": [], "tag_distribution": {},
        "last_errors": [],
    }


def test_prune_dry_run_then_yes_uses_rm_path(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = Catalog()
    for query in [
        Query("unused", "select 1"),
        Query("stale", "select 1", run_count=1, last_run="2000-01-01"),
        Query("preset", "select :n", presets={"one": {"n": 1}}),
        Query("base", "select 1"),
        Query("wrapper", "select * from {{ base }}"),
        Query("recent", "select 1", run_count=1, last_run="2999-01-01"),
        Query("unknown", "select 1", run_count=1, last_run="bad"),
    ]:
        catalog.add(query)
    original = catalog.path.read_bytes()
    removed = []
    remove = Catalog.remove

    def track_remove(self, name, *, save=True):
        removed.append(name)
        return remove(self, name, save=save)

    monkeypatch.setattr(Catalog, "remove", track_remove)
    preview = runner.invoke(app, ["prune"])
    assert preview.exit_code == 0, preview.stderr
    assert "Dry-run: 3 candidates" in preview.stdout
    assert "Protected base" in preview.stdout
    assert "Protected preset" in preview.stdout
    assert "Skipping unknown" in preview.stderr
    assert catalog.path.read_bytes() == original
    assert removed == []

    applied = runner.invoke(app, ["prune", "--yes"])
    assert applied.exit_code == 0, applied.stderr
    assert "Removed 3 queries" in applied.stdout
    assert removed == ["stale", "unused", "wrapper"]
    assert set(Catalog.load().names()) == {"preset", "base", "recent", "unknown"}
    # rm shares the persisted removal path without inheriting prune's guards.
    result = runner.invoke(app, ["rm", "preset", "--yes"])
    assert result.exit_code == 0, result.stderr
    assert removed[-1] == "preset"
    assert "preset" not in Catalog.load().names()


def test_prune_threshold_changes_selection_and_empty_pack_is_read_only() -> None:
    empty = runner.invoke(app, ["prune", "--yes"])
    assert empty.exit_code == 0, empty.stderr
    assert "Removed 0 queries" in empty.stdout
    assert not Catalog().path.exists()
    catalog = Catalog()
    catalog.add(Query("old", "select 1", run_count=1, last_run="2000-01-01"))
    preview = runner.invoke(app, ["prune", "--older-than", "0"])
    assert preview.exit_code == 0
    assert "Dry-run: 1 candidates" in preview.stdout
    result = runner.invoke(app, ["prune", "--older-than", "100000", "--yes"])
    assert result.exit_code == 0, result.stderr
    assert Catalog.load().names() == ["old"]


@pytest.mark.parametrize("args", [
    ["stats", "--top", "0"], ["stats", "--format", "csv"],
    ["prune", "--older-than", "-1"],
])
def test_invalid_options_do_not_change_pack(args: list[str]) -> None:
    catalog = Catalog()
    catalog.add(Query("keep", "select 1"))
    original = catalog.path.read_bytes()
    result = runner.invoke(app, args)
    assert result.exit_code != 0
    assert catalog.path.read_bytes() == original
