"""Tests for `quackpack schedule` (backlog #11 / issue #33).

Two layers:

* pure string-building & cron validation in :mod:`quackpack.schedule`
  (:func:`build_run_command`, :func:`build_cron_line`, :func:`validate_cron_expr`);
* crontab mutation (`install`/`--list`/`--remove`) driven against a *fake*
  crontab so no real crontab is ever touched.

The CLI is exercised via Typer's ``CliRunner`` against a throwaway
``QUACKPACK_HOME``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from quackpack import schedule as sched
from quackpack.cli import app
from quackpack.schedule import (
    CronError,
    SENTINEL,
    build_cron_line,
    build_run_command,
    install_line,
    list_lines,
    parse_managed,
    remove_line,
    validate_cron_expr,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("QUACKPACK_HOME", str(tmp_path))
    return tmp_path


def _add(name: str, sql: str = "select :region as r") -> None:
    res = runner.invoke(app, ["add", "-n", name, "-q", sql])
    assert res.exit_code == 0, res.stdout + res.stderr


# --------------------------------------------------------------------------
# cron expression validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    ["0 8 * * *", "*/15 9-17 * * 1-5", "0 0 1 1 0", "0,30 * * * *", "5 4 * * 7"],
)
def test_validate_accepts_good_exprs(expr: str) -> None:
    assert validate_cron_expr(expr) == " ".join(expr.split())


@pytest.mark.parametrize(
    "expr",
    ["bad", "0 8 * *", "0 8 * * * *", "60 8 * * *", "0 24 * * *", "0 8 0 * *", "0 8 * 13 *", "0 8 * * 8", "5-2 * * * *", "*/0 * * * *"],
)
def test_validate_rejects_bad_exprs(expr: str) -> None:
    with pytest.raises(CronError):
        validate_cron_expr(expr)


def test_validate_collapses_whitespace() -> None:
    assert validate_cron_expr("0   8 *  * *") == "0 8 * * *"


# --------------------------------------------------------------------------
# command / line building
# --------------------------------------------------------------------------


def test_build_run_command_basic() -> None:
    cmd = build_run_command("daily", file="data.parquet", out="out.csv")
    assert cmd == (
        "quackpack run daily --file data.parquet --format csv "
        "--no-input --no-snapshot > out.csv"
    )


def test_build_run_command_threads_all_options() -> None:
    cmd = build_run_command(
        "rep",
        db="w.duckdb",
        fmt="json",
        params=["region=west", "n=3"],
        preset="q3",
        out="r.json",
    )
    assert "--db w.duckdb" in cmd
    assert "--format json" in cmd
    assert "--preset q3" in cmd
    assert "--param region=west" in cmd
    assert "--param n=3" in cmd
    assert cmd.endswith("> r.json")


def test_build_run_command_shell_quotes_spaces() -> None:
    cmd = build_run_command("q", file="my data.parquet", out="the out.csv")
    assert "'my data.parquet'" in cmd
    assert "> 'the out.csv'" in cmd


def test_build_run_command_rejects_bad_format() -> None:
    with pytest.raises(CronError):
        build_run_command("q", fmt="xml")


def test_build_run_command_custom_bin() -> None:
    cmd = build_run_command("q", quackpack_bin="/opt/qp/quackpack")
    assert cmd.startswith("/opt/qp/quackpack run q")


def test_build_cron_line_appends_sentinel() -> None:
    line = build_cron_line("0 8 * * *", "quackpack run daily", name="daily")
    assert line == f"0 8 * * * quackpack run daily {SENTINEL} daily"


def test_build_cron_line_validates_expr() -> None:
    with pytest.raises(CronError):
        build_cron_line("nope", "cmd", name="x")


# --------------------------------------------------------------------------
# managed-line parsing + crontab operations (fake crontab)
# --------------------------------------------------------------------------


class FakeCrontab:
    """In-memory stand-in for the user's crontab."""

    def __init__(self, text: str = "") -> None:
        self.text = text

    def read(self) -> str:
        return self.text

    def write(self, text: str) -> None:
        self.text = text


def test_parse_managed_ignores_user_lines() -> None:
    lines = [
        "0 1 * * * /usr/bin/backup",  # user line, untouched
        f"0 8 * * * quackpack run a {SENTINEL} a",
        "# a comment",
        f"0 9 * * * quackpack run b {SENTINEL} b",
    ]
    managed = parse_managed(lines)
    assert [m.name for m in managed] == ["a", "b"]
    assert [m.index for m in managed] == [0, 1]


def test_install_line_is_idempotent() -> None:
    fake = FakeCrontab("0 1 * * * /usr/bin/backup\n")
    line = f"0 8 * * * quackpack run a {SENTINEL} a"
    assert install_line(line, reader=fake.read, writer=fake.write) is True
    # User line preserved.
    assert "/usr/bin/backup" in fake.text
    assert line in fake.text
    # Second install is a no-op.
    assert install_line(line, reader=fake.read, writer=fake.write) is False
    assert fake.text.count(line) == 1


def test_list_lines_only_managed() -> None:
    fake = FakeCrontab(
        "0 1 * * * /usr/bin/backup\n"
        f"0 8 * * * quackpack run a {SENTINEL} a\n"
    )
    got = list_lines(reader=fake.read)
    assert len(got) == 1 and got[0].name == "a"


def test_remove_line_by_name_preserves_others() -> None:
    fake = FakeCrontab(
        "0 1 * * * /usr/bin/backup\n"
        f"0 8 * * * quackpack run a {SENTINEL} a\n"
        f"0 9 * * * quackpack run b {SENTINEL} b\n"
    )
    removed = remove_line(name="a", reader=fake.read, writer=fake.write)
    assert len(removed) == 1 and removed[0].name == "a"
    assert "/usr/bin/backup" in fake.text
    assert "run b" in fake.text
    assert "run a" not in fake.text


def test_remove_line_by_index() -> None:
    fake = FakeCrontab(
        f"0 8 * * * quackpack run a {SENTINEL} a\n"
        f"0 9 * * * quackpack run b {SENTINEL} b\n"
    )
    removed = remove_line(index=1, reader=fake.read, writer=fake.write)
    assert removed[0].name == "b"
    assert "run b" not in fake.text
    assert "run a" in fake.text


def test_remove_line_unknown_name_raises() -> None:
    fake = FakeCrontab("")
    with pytest.raises(CronError):
        remove_line(name="ghost", reader=fake.read, writer=fake.write)


def test_remove_line_requires_selector() -> None:
    fake = FakeCrontab("")
    with pytest.raises(CronError):
        remove_line(reader=fake.read, writer=fake.write)


# --------------------------------------------------------------------------
# CLI integration
# --------------------------------------------------------------------------


def test_cli_schedule_prints_line() -> None:
    _add("daily")
    res = runner.invoke(
        app, ["schedule", "daily", "--at", "0 8 * * *", "-f", "data.parquet", "-o", "out.csv"]
    )
    assert res.exit_code == 0, res.stdout + res.stderr
    assert res.stdout.strip() == (
        "0 8 * * * quackpack run daily --file data.parquet --format csv "
        f"--no-input --no-snapshot > out.csv {SENTINEL} daily"
    )


def test_cli_schedule_unknown_query_fails() -> None:
    res = runner.invoke(app, ["schedule", "nope"])
    assert res.exit_code == 1
    assert "no query named 'nope'" in res.stderr.lower()


def test_cli_schedule_bad_expr_fails() -> None:
    _add("daily")
    res = runner.invoke(app, ["schedule", "daily", "--at", "nope"])
    assert res.exit_code == 1
    assert "cron" in res.stderr.lower()


def test_cli_schedule_install_and_list_and_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    _add("daily")
    fake = FakeCrontab("0 1 * * * /usr/bin/backup\n")
    monkeypatch.setattr(sched, "read_crontab", fake.read)
    monkeypatch.setattr(sched, "write_crontab", fake.write)

    res = runner.invoke(app, ["schedule", "daily", "--at", "0 8 * * *", "-o", "o.csv", "--install", "--yes"])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "Installed" in res.stdout
    assert "/usr/bin/backup" in fake.text  # user line untouched

    # Idempotent second install.
    res = runner.invoke(app, ["schedule", "daily", "--at", "0 8 * * *", "-o", "o.csv", "--install", "--yes"])
    assert res.exit_code == 0
    assert "already present" in res.stdout

    # List shows only the managed line.
    res = runner.invoke(app, ["schedule", "daily", "--list"])
    assert res.exit_code == 0
    assert "daily" in res.stdout
    assert "backup" not in res.stdout

    # Remove drops it, keeps the user line.
    res = runner.invoke(app, ["schedule", "daily", "--remove", "--yes"])
    assert res.exit_code == 0
    assert "Removed 1" in res.stdout
    assert "/usr/bin/backup" in fake.text
    assert "run daily" not in fake.text


def test_cli_schedule_install_needs_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    _add("daily")
    fake = FakeCrontab("")
    monkeypatch.setattr(sched, "read_crontab", fake.read)
    monkeypatch.setattr(sched, "write_crontab", fake.write)
    # No --yes and no TTY -> aborts without touching crontab.
    res = runner.invoke(app, ["schedule", "daily", "--install"])
    assert res.exit_code == 1
    assert "aborted" in res.stderr.lower()
    assert fake.text == ""
