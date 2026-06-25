"""Integration tests for M4 parameters: typing, explicit hints, and prompts.

Builds on the M3 ``run`` plumbing but focuses on what M4 adds:

* ``--param`` values are coerced to int/float/str (so numeric filters compare
  numerically), including via an explicit ``key:type`` hint;
* a declared ``:param`` that the caller omits is prompted for interactively when
  stdin is a TTY, and the typed answer is bound;
* non-interactive runs keep the M3 "warn, then let the engine complain"
  behaviour so pipes/CI never hang.

All tests drive the real Typer app against a throwaway ``QUACKPACK_HOME``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quackpack import cli, engine
from quackpack.cli import app

runner = CliRunner()

needs_duckdb = pytest.mark.skipif(
    not engine.DUCKDB_AVAILABLE, reason="duckdb not installed"
)


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("QUACKPACK_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def sales_csv(tmp_path: Path) -> Path:
    p = tmp_path / "sales.csv"
    p.write_text(
        "region,amount,rate\nwest,100,0.2\neast,250,0.5\nwest,75,0.1\n",
        encoding="utf-8",
    )
    return p


def _add(name: str, sql: str) -> None:
    res = runner.invoke(app, ["add", "-n", name, "-q", sql])
    assert res.exit_code == 0, res.stdout


# --------------------------------------------------------------------------
# Typed --param binding
# --------------------------------------------------------------------------


@needs_duckdb
def test_int_param_filters_numerically_duckdb(sales_csv: Path) -> None:
    # If "90" were bound as text, ``amount > '90'`` would compare lexically and
    # drop 100. Coercion to int keeps it correct.
    _add("big", "select amount from sales where amount > :min order by amount")
    res = runner.invoke(
        app, ["run", "big", "--file", str(sales_csv), "-p", "min=90", "-F", "json"]
    )
    assert res.exit_code == 0, res.stdout
    assert json.loads(res.stdout) == [{"amount": 100}, {"amount": 250}]


def test_int_param_filters_numerically_sqlite(sales_csv: Path) -> None:
    _add("big", "select amount from sales where amount > :min order by amount")
    res = runner.invoke(
        app,
        ["run", "big", "--file", str(sales_csv), "-p", "min=90", "-e", "sqlite", "-F", "csv"],
    )
    assert res.exit_code == 0, res.stdout
    body = [ln for ln in res.stdout.strip().splitlines() if ln]
    assert body == ["amount", "100", "250"]


@needs_duckdb
def test_float_param_binds_as_float(sales_csv: Path) -> None:
    _add("byrate", "select amount from sales where rate >= :r order by amount")
    res = runner.invoke(
        app, ["run", "byrate", "--file", str(sales_csv), "-p", "r=0.2", "-F", "json"]
    )
    assert res.exit_code == 0, res.stdout
    assert json.loads(res.stdout) == [{"amount": 100}, {"amount": 250}]


@needs_duckdb
def test_string_param_binds_as_text(sales_csv: Path) -> None:
    _add("byregion", "select sum(amount) as total from sales where region = :reg")
    res = runner.invoke(
        app, ["run", "byregion", "--file", str(sales_csv), "-p", "reg=east", "-F", "json"]
    )
    assert res.exit_code == 0, res.stdout
    assert json.loads(res.stdout) == [{"total": 250}]


@needs_duckdb
def test_explicit_str_hint_keeps_leading_zero(sales_csv: Path) -> None:
    # "00100" would auto-coerce to int 100 and match; forcing :str keeps it text
    # so it matches nothing (region is text, not numeric).
    _add("byregion", "select count(*) as n from sales where region = :reg")
    res = runner.invoke(
        app,
        ["run", "byregion", "--file", str(sales_csv), "-p", "reg:str=00100", "-F", "json"],
    )
    assert res.exit_code == 0, res.stdout
    assert json.loads(res.stdout) == [{"n": 0}]


def test_bad_explicit_cast_is_reported(sales_csv: Path) -> None:
    _add("big", "select amount from sales where amount > :min")
    res = runner.invoke(
        app, ["run", "big", "--file", str(sales_csv), "-p", "min:int=not_a_number"]
    )
    assert res.exit_code == 1
    assert "not a valid int" in res.stderr


# --------------------------------------------------------------------------
# Interactive prompting for missing params
# --------------------------------------------------------------------------


def test_missing_param_prompts_when_interactive(
    sales_csv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pretend we're on a TTY and feed a prompt answer; it should be typed (int)
    # and bound, producing the same result as passing --param.
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    prompts: list[str] = []

    def fake_prompt(text: str):
        prompts.append(text)
        return "90"

    monkeypatch.setattr(cli.typer, "prompt", fake_prompt)

    _add("big", "select amount from sales where amount > :min order by amount")
    res = runner.invoke(
        app, ["run", "big", "--file", str(sales_csv), "-e", "sqlite", "-F", "csv"]
    )
    assert res.exit_code == 0, res.stdout + res.stderr
    assert any("min" in p for p in prompts)  # we actually prompted
    body = [ln for ln in res.stdout.strip().splitlines() if ln]
    assert body == ["amount", "100", "250"]


def test_prompt_only_for_missing_params(
    sales_csv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One param supplied via --param, one omitted: only the omitted one prompts.
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    asked: list[str] = []

    def fake_prompt(text: str):
        asked.append(text)
        return "200"

    monkeypatch.setattr(cli.typer, "prompt", fake_prompt)

    _add(
        "between",
        "select amount from sales where amount >= :lo and amount <= :hi order by amount",
    )
    res = runner.invoke(
        app,
        ["run", "between", "--file", str(sales_csv), "-p", "lo=80", "-e", "sqlite", "-F", "csv"],
    )
    assert res.exit_code == 0, res.stdout + res.stderr
    # Only :hi should have been prompted for.
    assert len(asked) == 1 and "hi" in asked[0]
    body = [ln for ln in res.stdout.strip().splitlines() if ln]
    assert body == ["amount", "100"]


def test_missing_param_non_interactive_warns_not_prompts(
    sales_csv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Non-TTY: must NOT call prompt (would hang); warns and the engine errors.
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: False)

    def boom(text: str):  # pragma: no cover - should never run
        raise AssertionError("prompt() must not be called when non-interactive")

    monkeypatch.setattr(cli.typer, "prompt", boom)

    _add("needs", "select amount from sales where amount > :min")
    res = runner.invoke(app, ["run", "needs", "--file", str(sales_csv), "-e", "sqlite"])
    assert "warning" in res.stderr.lower()
    assert res.exit_code == 1
