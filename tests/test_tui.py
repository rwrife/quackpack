"""The browser's models remain usable without the optional Textual package."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import pytest

from quackpack.store import Catalog, Query
from quackpack.tui import build_query_list, resolve_run


def test_query_list_uses_store_search_and_exact_tag() -> None:
    class FakeStore:
        def search(self, text: str) -> list[Query]:
            assert text == "revenue"
            return [
                Query("east", "SELECT 1", tags=["finance"], last_run="2026-01-01T10:00:00+00:00"),
                Query("west", "SELECT 2", tags=["finance-team"]),
            ]

    items = build_query_list(
        FakeStore(), "revenue", " finance ",
        now=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
    )
    assert [(item.name, item.tags, item.last_run) for item in items] == [
        ("east", ("finance",), "2h ago")
    ]


def test_query_list_searches_description_sql_tags_and_handles_empty(tmp_path: Path) -> None:
    catalog = Catalog(path=tmp_path / "pack.yaml", queries=[
        Query("z", "SELECT 1", desc="Revenue report"),
        Query("a", "SELECT revenue", tags=["finance"]),
    ])
    assert [item.name for item in build_query_list(catalog, "REVENUE")] == ["a", "z"]
    assert [item.name for item in build_query_list(catalog, "finance")] == ["a"]
    assert build_query_list(catalog, "missing") == []
    assert all(item.last_run == "never" for item in build_query_list(catalog))


def test_resolution_coerces_values_and_distinguishes_empty_from_missing() -> None:
    supplied = {"n": "42", "ratio": "2.5", "text": "", "extra": "ignored"}
    request = resolve_run(
        "SELECT :n, :ratio, :text, :missing, :n, ':literal'",
        db=" data.duckdb ", file=" sales.csv ", engine=" SQLITE ", values=supplied,
    )
    assert request.db == Path("data.duckdb")
    assert request.file == Path("sales.csv")
    assert request.engine == "sqlite"
    assert request.params == {"n": 42, "ratio": 2.5, "text": ""}
    assert request.missing == ("missing",)
    assert supplied["n"] == "42"


def test_resolution_allows_memory_and_expands_home() -> None:
    request = resolve_run("SELECT 1", db="  ", file="~/sales.csv")
    assert request.db is None
    assert request.file == Path("~/sales.csv").expanduser()
    assert request.params == {}
    assert request.missing == ()
    assert resolve_run("SELECT :n").missing == ("n",)
    with pytest.raises(ValueError, match="Unknown engine"):
        resolve_run("SELECT 1", engine="unknown")


@pytest.mark.parametrize("target", [
    "postgres://reader:example-password@localhost/warehouse",
    "mysql://reader:example-password@localhost/warehouse",
    "@warehouse",
])
def test_resolution_preserves_target_without_looking_up_connections(
    target: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quackpack.connections import Connections

    def unexpected_lookup(*args: object) -> str:
        pytest.fail("Pure resolution must not read connection storage")

    monkeypatch.setattr(Connections, "get", unexpected_lookup)
    request = resolve_run("SELECT :n", target=target, values={"n": "5"})
    assert request.target == target
    assert request.db is None
    assert request.file is None
    assert request.params == {"n": 5}
    assert "example-password" not in repr(request)
    with pytest.raises(ValueError, match="cannot be combined"):
        resolve_run("SELECT 1", target=target, db="local.duckdb")


def test_cli_help_and_install_hint_without_textual() -> None:
    script = """
import importlib.abc
import sys
class NoTextual(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "textual" or fullname.startswith("textual."):
            raise ModuleNotFoundError("Textual intentionally absent", name=fullname)
sys.meta_path.insert(0, NoTextual())
from quackpack.cli import app
from quackpack.tui import resolve_run
from typer.testing import CliRunner
runner = CliRunner()
for args in (["--help"], ["tui", "--help"]):
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
assert not any(name.startswith("textual") for name in sys.modules)
result = runner.invoke(app, ["tui"])
assert result.exit_code == 1, result.output
assert "pip install 'quackpack[tui]'" in result.output, result.output
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_textual_filter_preview_prompt_run_and_help(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Input, Static

    from quackpack.engine import QueryResult
    from quackpack.snapshots import Snapshot, load_snapshot, save_snapshot
    from quackpack.tui_app import HelpScreen, ParamPrompt, QuackpackApp

    monkeypatch.setenv("QUACKPACK_HOME", str(tmp_path))
    data = tmp_path / "sales.csv"
    data.write_text("label,amount\nsmall,1\nlarge,10\n", encoding="utf-8")
    catalog = Catalog(queries=[
        Query("report", "SELECT label FROM sales WHERE amount >= :minimum",
              tags=["demo"], desc="A filtered report"),
        Query("other", "SELECT 99", tags=["other"]),
    ])
    save_snapshot(Snapshot.from_result("report", QueryResult(["label"], [("old",)])))

    async def exercise() -> None:
        app = QuackpackApp(catalog, file=str(data), engine="sqlite", tag="demo")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.selected_name == "report"
            assert app.query_one("#queries", DataTable).row_count == 1
            assert ":minimum" in str(app.query_one("#metadata", Static).render())
            results = app.query_one("#results", DataTable)
            assert results.get_row_at(0)[0].plain == "old"
            app.query_one("#filter", Input).value = "no matches"
            await pilot.pause()
            assert app.selected_name is None
            assert results.row_count == 0
            app.query_one("#filter", Input).value = "filtered"
            await pilot.pause()
            assert app.selected_name == "report"
            await pilot.press("ctrl+r")
            await pilot.pause()
            assert isinstance(app.screen, ParamPrompt)
            app.screen.query_one(Input).value = "5"
            await pilot.click("#confirm")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert results.get_row_at(0)[0].plain == "large"
            assert catalog.get("report").last_status == "ok"
            snapshot = load_snapshot("report")
            assert snapshot is not None
            assert snapshot.rows == [("large",)]
            assert snapshot.params == {"minimum": 5}
            app.query_one("#queries", DataTable).focus()
            await pilot.press("?")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, HelpScreen)

    asyncio.run(exercise())


def test_textual_forwards_target_to_existing_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Input

    from quackpack.engine import QueryResult
    from quackpack import tui_app

    monkeypatch.setenv("QUACKPACK_HOME", str(tmp_path))
    calls = []

    def fake_run(sql: str, **kwargs: object) -> QueryResult:
        calls.append((sql, kwargs))
        return QueryResult(["value"], [(9,)])

    monkeypatch.setattr(tui_app, "run_query", fake_run)

    async def exercise() -> None:
        app = tui_app.QuackpackApp(
            Catalog(queries=[Query("report", "SELECT 9 AS value")]),
            target="@warehouse",
        )
        async with app.run_test(size=(100, 35)) as pilot:
            assert app.query_one("#target", Input).password
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert calls == [("SELECT 9 AS value", {
                "db": None, "file": None, "engine": "auto",
                "params": {}, "target": "@warehouse",
            })]
            assert app.query_one("#results", DataTable).get_row_at(0)[0].plain == "9"

    asyncio.run(exercise())


def test_textual_cancel_and_execution_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Input, Static

    from quackpack.snapshots import load_snapshot
    from quackpack.tui_app import ParamPrompt, QuackpackApp

    monkeypatch.setenv("QUACKPACK_HOME", str(tmp_path))
    catalog = Catalog(queries=[Query("broken", "SELECT :n FROM missing_table")])

    async def exercise() -> None:
        app = QuackpackApp(catalog, engine="sqlite")
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ParamPrompt)
            await pilot.press("escape")
            await app.workers.wait_for_complete()
            assert catalog.get("broken").run_count == 0
            await pilot.press("ctrl+r")
            await pilot.pause()
            app.screen.query_one(Input).value = "1"
            await pilot.click("#confirm")
            await app.workers.wait_for_complete()
            assert catalog.get("broken").last_status == "error"
            assert "Error:" in str(app.query_one("#status", Static).render())
            assert load_snapshot("broken") is None

    asyncio.run(exercise())


def test_textual_template_params_and_selection_survive_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Input, Static

    from quackpack.snapshots import load_snapshot
    from quackpack.tui_app import ParamPrompt, QuackpackApp

    monkeypatch.setenv("QUACKPACK_HOME", str(tmp_path))
    catalog = Catalog(queries=[
        Query("base", "SELECT :n AS value"),
        Query("report", "SELECT * FROM {{ base }}"),
    ])

    async def exercise() -> None:
        app = QuackpackApp(catalog, engine="sqlite")
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.press("down")
            await pilot.pause()
            assert app.selected_name == "report"
            assert ":n" in str(app.query_one("#metadata", Static).render())
            app.refresh_queries()
            await pilot.pause()
            assert app.selected_name == "report"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ParamPrompt)
            app.screen.query_one(Input).value = "7"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.selected_name == "report"
            assert app.query_one("#results", DataTable).get_row_at(0)[0].plain == "7"
            snapshot = load_snapshot("report")
            assert snapshot is not None
            assert snapshot.rows == [(7,)]
            assert Catalog.load().get("report").run_count == 1

    asyncio.run(exercise())
