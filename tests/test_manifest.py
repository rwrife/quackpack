"""Tests for the agent/MCP tool manifest (backlog #6 / issue #26).

Covers:

* ``manifest.build_manifest`` / ``tool_entry`` shape for both ``json`` and
  ``jsonschema``, including a query mixing required / optional / preset-defaulted
  params, and ``--tag`` filtering.
* The ``tools`` and ``describe`` CLI commands.
* The non-interactive ``run`` contract: ``--no-input`` / ``QUACKPACK_NO_INPUT``
  never prompt, missing required params exit 1 with an ``error:`` on stderr, and
  a satisfied run emits the documented ``{columns, rows, rowcount}`` envelope.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from quackpack.cli import app
from quackpack.manifest import build_manifest, query_params, tool_entry
from quackpack.store import Catalog, Query

runner = CliRunner()


# --------------------------------------------------------------------------
# Pure manifest generation
# --------------------------------------------------------------------------


def _mixed_query() -> Query:
    """A query with an int preset-default param and a required str param."""
    q = Query(
        name="orders",
        sql="select * from data where amount > :min and region = :region",
        desc="Big orders in a region",
        tags=["sales"],
    )
    # A preset supplies a default for ``min`` (int) but not ``region``.
    q.set_preset("q3", {"min": 100})
    return q


def test_query_params_marks_required_and_defaulted() -> None:
    params = {p["name"]: p for p in query_params(_mixed_query())}
    assert params["min"] == {
        "name": "min",
        "type": "int",
        "required": False,
        "default": 100,
    }
    assert params["region"] == {
        "name": "region",
        "type": "str",
        "required": True,
    }


def test_tool_entry_json_shape() -> None:
    entry = tool_entry(_mixed_query(), fmt="json")
    assert entry["name"] == "orders"
    assert entry["description"] == "Big orders in a region"
    assert [p["name"] for p in entry["params"]] == ["min", "region"]


def test_tool_entry_jsonschema_shape() -> None:
    entry = tool_entry(_mixed_query(), fmt="jsonschema")
    schema = entry["inputSchema"]
    assert schema["type"] == "object"
    assert schema["properties"]["min"] == {"type": "integer", "default": 100}
    assert schema["properties"]["region"] == {"type": "string"}
    assert schema["required"] == ["region"]
    assert schema["additionalProperties"] is False


def test_build_manifest_tag_filter_and_order() -> None:
    cat = Catalog(queries=[
        Query(name="zeta", sql="select 1", tags=["demo"]),
        Query(name="alpha", sql="select 2", tags=["sales"]),
    ])
    # Sorted by name; unfiltered has both.
    assert [t["name"] for t in build_manifest(cat)] == ["alpha", "zeta"]
    # Tag filter narrows it.
    sales = build_manifest(cat, tag="sales")
    assert [t["name"] for t in sales] == ["alpha"]


# --------------------------------------------------------------------------
# CLI: tools / describe
# --------------------------------------------------------------------------


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("QUACKPACK_HOME", str(tmp_path))
    # Ensure no stray env var leaks in from the runner's environment.
    monkeypatch.delenv("QUACKPACK_NO_INPUT", raising=False)
    return tmp_path


def _add(name: str, sql: str, *args: str) -> None:
    res = runner.invoke(app, ["add", "-n", name, "-q", sql, *args])
    assert res.exit_code == 0, res.output


def test_tools_command_json(home) -> None:
    _add("big", "select * from data where amount > :min", "-d", "Big", "-t", "sales")
    res = runner.invoke(app, ["tools"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data == [
        {
            "name": "big",
            "description": "Big",
            "params": [{"name": "min", "type": "str", "required": True}],
        }
    ]


def test_tools_command_jsonschema_and_tag(home) -> None:
    _add("big", "select * from data where amount > :min", "-t", "sales")
    _add("plain", "select 1", "-t", "other")
    res = runner.invoke(app, ["tools", "--format", "jsonschema", "--tag", "sales"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert [t["name"] for t in data] == ["big"]
    assert data[0]["inputSchema"]["required"] == ["min"]


def test_tools_bad_format_exits_1(home) -> None:
    res = runner.invoke(app, ["tools", "--format", "yaml"])
    assert res.exit_code == 1
    assert "Unknown --format" in res.output


def test_describe_matches_tools_entry(home) -> None:
    _add("big", "select * from data where amount > :min", "-d", "Big")
    tools = json.loads(runner.invoke(app, ["tools"]).stdout)
    one = json.loads(runner.invoke(app, ["describe", "big"]).stdout)
    assert one == tools[0]


def test_describe_unknown_exits_1(home) -> None:
    res = runner.invoke(app, ["describe", "nope"])
    assert res.exit_code == 1
    assert "No query named" in res.output


# --------------------------------------------------------------------------
# CLI: non-interactive run contract
# --------------------------------------------------------------------------


def _csv(tmp_path) -> str:
    p = tmp_path / "data.csv"
    p.write_text("region,amount\nwest,100\neast,250\n", encoding="utf-8")
    return str(p)


def test_run_no_input_envelope(home, tmp_path) -> None:
    _add("big", "select * from data where amount > :min")
    res = runner.invoke(
        app,
        ["run", "big", "--file", _csv(tmp_path), "--param", "min=50",
         "--format", "json", "--no-input"],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["columns"] == ["region", "amount"]
    assert payload["rowcount"] == 2
    assert ["west", 100] in payload["rows"]


def test_run_no_input_missing_param_exits_1(home, tmp_path) -> None:
    _add("big", "select * from data where amount > :min")
    res = runner.invoke(
        app,
        ["run", "big", "--file", _csv(tmp_path), "--format", "json", "--no-input"],
    )
    assert res.exit_code == 1
    assert "error:" in res.output
    assert "min" in res.output


def test_run_env_no_input(home, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QUACKPACK_NO_INPUT", "1")
    _add("big", "select * from data where amount > :min")
    res = runner.invoke(
        app,
        ["run", "big", "--file", _csv(tmp_path), "--format", "json"],
    )
    assert res.exit_code == 1
    assert "min" in res.output


def test_run_json_without_no_input_stays_array(home, tmp_path) -> None:
    # Backwards-compat: plain `--format json` (no --no-input/--envelope) still
    # emits the array-of-objects shape.
    _add("big", "select * from data where amount > :min")
    res = runner.invoke(
        app,
        ["run", "big", "--file", _csv(tmp_path), "--param", "min=50",
         "--format", "json"],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert isinstance(data, list)
    assert data[0] == {"region": "west", "amount": 100}


def test_run_envelope_flag_without_no_input(home, tmp_path) -> None:
    _add("big", "select * from data where amount > :min")
    res = runner.invoke(
        app,
        ["run", "big", "--file", _csv(tmp_path), "--param", "min=50",
         "--format", "json", "--envelope"],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert set(payload) == {"columns", "rows", "rowcount"}
