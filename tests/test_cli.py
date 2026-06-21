"""Smoke tests for the M1 scaffold.

These intentionally cover only what M1 ships: the package imports, the
version is exposed, and the ``--version`` / ``hello`` commands work.
"""

from __future__ import annotations

import re

from typer.testing import CliRunner

import quackpack
from quackpack.cli import app

runner = CliRunner()


def test_package_exposes_version() -> None:
    assert isinstance(quackpack.__version__, str)
    # Looks like a sane semver-ish string, e.g. "0.1.0".
    assert re.match(r"^\d+\.\d+", quackpack.__version__)


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "quackpack" in result.stdout
    assert quackpack.__version__ in result.stdout


def test_hello_default() -> None:
    result = runner.invoke(app, ["hello"])
    assert result.exit_code == 0
    assert "hello" in result.stdout.lower()
    assert "world" in result.stdout


def test_hello_named() -> None:
    result = runner.invoke(app, ["hello", "--name", "ducky"])
    assert result.exit_code == 0
    assert "ducky" in result.stdout


def test_no_args_shows_help() -> None:
    # no_args_is_help -> usage text is shown and a non-zero "missing command"
    # exit code is returned (Typer/Click convention).
    result = runner.invoke(app, [])
    assert result.exit_code != 0
    assert "Usage" in result.stdout
