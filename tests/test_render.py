"""Tests for :mod:`quackpack.render` output strategies."""

from __future__ import annotations

import json

from rich.console import Console

from quackpack.engine import QueryResult
from quackpack.render import render, render_csv, render_json


def _result() -> QueryResult:
    return QueryResult(columns=["name", "n"], rows=[("a", 1), ("b", None)])


def test_render_csv_has_header_and_blank_for_null() -> None:
    out = render_csv(_result())
    lines = out.strip().splitlines()
    assert lines[0] == "name,n"
    assert lines[1] == "a,1"
    # None serialises to an empty field.
    assert lines[2] == "b,"


def test_render_json_maps_columns_and_null() -> None:
    data = json.loads(render_json(_result()))
    assert data == [{"name": "a", "n": 1}, {"name": "b", "n": None}]


def test_render_table_prints_columns_and_footer() -> None:
    console = Console(record=True, width=80)
    render(_result(), "table", console)
    text = console.export_text()
    assert "name" in text and "n" in text
    assert "a" in text and "b" in text
    assert "2 rows" in text
    # NULL marker for the None cell.
    assert "NULL" in text


def test_render_single_row_footer_singular() -> None:
    console = Console(record=True, width=80)
    render(QueryResult(columns=["x"], rows=[(1,)]), "table", console)
    assert "1 row" in console.export_text()


def test_render_csv_via_dispatch_no_markup() -> None:
    console = Console(record=True, width=80)
    render(_result(), "csv", console)
    out = console.export_text()
    assert "name,n" in out
