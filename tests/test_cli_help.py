"""Guards for the M6 `--help` polish and exit-code consistency.

These lock in two user-facing contracts that are easy to regress:

1. **Clean help text.** Command help is rendered as Markdown, so docstrings must
   use single-backtick inline code. A stray RST ``double-backtick`` (or dropping
   ``rich_markup_mode``) would leak literal backticks into ``--help``; we assert
   none survive in any command's rendered help.
2. **Consistent exit codes.** quackpack follows the usual CLI convention:
   ``0`` on success (including empty result sets), ``1`` for runtime/user errors
   (uniform ``error:`` prefix), and ``2`` for usage errors (missing args, via
   Click). These tests pin that contract so error handling stays predictable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from quackpack.cli import app

runner = CliRunner()

# Every command that takes ``--help``. ``hello`` is the M1 smoke command; the
# rest are the real workflow. Keeping this list explicit means a newly added
# command with sloppy help markup trips the test until it's listed and clean.
COMMANDS = ["hello", "add", "ls", "show", "search", "edit", "run", "tools", "describe", "pipe", "rm", "export", "import"]


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the catalog at an isolated dir so tests never touch a real pack."""
    monkeypatch.setenv("QUACKPACK_HOME", str(tmp_path))
    return tmp_path


def test_top_level_help_is_clean() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    # No leaked RST inline-code markup in the rendered help.
    assert "``" not in result.output
    # The command list is present.
    for name in COMMANDS:
        assert name in result.output


@pytest.mark.parametrize("command", COMMANDS)
def test_command_help_is_clean(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0
    # Markdown mode should consume single backticks; a literal double-backtick
    # means a docstring still uses RST markup (or markup mode regressed).
    assert "``" not in result.output, f"{command} --help leaked literal ``markup``"
    # Sanity: the command's own usage line shows up.
    assert command in result.output


# --------------------------------------------------------------------------
# Exit-code contract
# --------------------------------------------------------------------------


def test_unknown_query_is_error_exit_1(home: Path) -> None:
    result = runner.invoke(app, ["show", "does-not-exist"])
    assert result.exit_code == 1
    assert "error:" in result.output.lower()


def test_run_unknown_query_is_error_exit_1(home: Path) -> None:
    result = runner.invoke(app, ["run", "nope"])
    assert result.exit_code == 1
    assert "error:" in result.output.lower()


def test_bad_format_is_error_exit_1(home: Path) -> None:
    runner.invoke(app, ["add", "-n", "c", "-q", "SELECT 1 AS x"])
    result = runner.invoke(app, ["run", "c", "--format", "xml"])
    assert result.exit_code == 1
    assert "error:" in result.output.lower()


def test_bad_engine_is_error_exit_1(home: Path) -> None:
    runner.invoke(app, ["add", "-n", "c", "-q", "SELECT 1 AS x"])
    result = runner.invoke(app, ["run", "c", "--engine", "mysql"])
    assert result.exit_code == 1
    assert "error:" in result.output.lower()


def test_bad_param_is_error_exit_1(home: Path) -> None:
    runner.invoke(app, ["add", "-n", "c", "-q", "SELECT 1 AS x"])
    result = runner.invoke(app, ["run", "c", "--param", "noequalshere"])
    assert result.exit_code == 1
    assert "error:" in result.output.lower()


def test_duplicate_add_is_error_exit_1(home: Path) -> None:
    first = runner.invoke(app, ["add", "-n", "dup", "-q", "SELECT 1"])
    assert first.exit_code == 0
    second = runner.invoke(app, ["add", "-n", "dup", "-q", "SELECT 2"])
    assert second.exit_code == 1
    assert "error:" in second.output.lower()


def test_missing_required_arg_is_usage_exit_2() -> None:
    # No NAME argument -> Click usage error, conventionally exit code 2.
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 2


def test_empty_search_is_success_exit_0(home: Path) -> None:
    # An empty result set is not an error; search should exit 0.
    result = runner.invoke(app, ["search", "no-such-text"])
    assert result.exit_code == 0


def test_empty_ls_is_success_exit_0(home: Path) -> None:
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
