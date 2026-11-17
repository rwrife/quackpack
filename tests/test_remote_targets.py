"""Remote targets without network access; live scanner smoke tests are opt-in."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from quackpack import engine
from quackpack.cli import app
from quackpack.connections import Connections
from quackpack.targets import TargetError, parse_target, resolve_options

runner = CliRunner()
DSN = "postgres://analyst:p%40ss%27word@localhost/shop?sslpassword=private"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("QUACKPACK_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def scanner(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    con = MagicMock()
    con.execute.return_value = con
    con.description = [("n",)]
    con.fetchall.return_value = [(1,)]
    monkeypatch.setattr(engine, "DUCKDB_AVAILABLE", True)
    monkeypatch.setattr(engine, "_duckdb", SimpleNamespace(connect=lambda **_: con))
    return con


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("postgres://user@host/db", "postgres"),
        ("postgresql://user@[::1]:5432/db?sslmode=require", "postgres"),
        ("mysql://user:pass@host:3306/db?ssl_mode=required", "mysql"),
        ("sales.csv", "file"),
        ("sales.parquet", "file"),
        ("logs/*.csv", "file"),
        ("shop.duckdb", "db"),
        (r"C:\data\shop.sqlite3", "db"),
    ],
)
def test_parse_target(value: str, kind: str) -> None:
    assert parse_target(value).kind == kind


@pytest.mark.parametrize(
    "dsn",
    [
        "", "postgres://", "postgres://user:secret@host:bad/db",
        "postgres://user:secret@host:99999/db", "mysql://user:secret@host/db#fragment",
        "mysql://user:secret@host/db?password=override",
        "postgres://user:secret@host/db%zz", "mysql://user:sec%0aret@host/db",
        "https://user:secret@host/db", "postgres://[broken",
    ],
)
def test_invalid_target_does_not_echo_input(dsn: str) -> None:
    with pytest.raises(TargetError) as error:
        parse_target(dsn)
    assert "secret" not in str(error.value)
    assert "sec%0aret" not in str(error.value)


def test_redaction_hides_encoded_credentials_and_query_values() -> None:
    target = parse_target(DSN)
    assert target.redacted() == (
        "postgres://<redacted>@localhost/shop?<redacted>"
    )
    assert DSN not in repr(target)
    assert "private" not in target.redacted()
    assert "analyst" not in target.redacted()


def test_postgres_attach_escapes_sql_and_enforces_read_only() -> None:
    target = parse_target("postgres://user:p'ass@host/db")
    assert engine._remote_attach_sql(target) == (
        "ATTACH 'postgres://user:p''ass@host/db' AS remote (TYPE postgres, READ_ONLY)"
    )


def test_mysql_uri_becomes_scanner_dsn_with_two_layers_of_escaping() -> None:
    target = parse_target(
        "mysql://user:p%27a%22%5Css@host:3306/my%20db?ssl_mode=required"
    )
    assert target.scanner_dsn() == (
        'host="host" port="3306" user="user" password="p\'a\\"\\\\ss" '
        'database="my db" ssl_mode="required"'
    )
    sql = engine._remote_attach_sql(target)
    assert sql == (
        "ATTACH 'host=\"host\" port=\"3306\" user=\"user\" password=\"p''a\\\"\\\\ss\" "
        "database=\"my db\" ssl_mode=\"required\"' AS remote (TYPE mysql, READ_ONLY)"
    )


def test_connections_roundtrip_is_separate_and_private(home: Path) -> None:
    connections = Connections()
    connections.add("prod", DSN)
    assert connections.get("prod") == DSN
    assert resolve_options("@prod", None, None)[2] == parse_target(DSN)
    assert not (home / "pack.yaml").exists()
    if os.name != "nt":
        assert stat.S_IMODE(connections.path.stat().st_mode) == 0o600
    with pytest.raises(TargetError, match="already exists"):
        connections.add("prod", DSN)
    connections.remove("prod")
    assert Connections().load() == {}
    with pytest.raises(TargetError, match="No connection"):
        connections.get("prod")
    with pytest.raises(TargetError, match="No connection"):
        connections.remove("prod")


def test_connections_reject_invalid_names_local_dsns_and_corrupt_storage(home: Path) -> None:
    connections = Connections()
    with pytest.raises(TargetError, match="names"):
        connections.add("../prod", DSN)
    with pytest.raises(TargetError, match="Named connections"):
        connections.add("prod", "shop.csv")
    connections.path.write_text('{"prod": "password=secret",', encoding="utf-8")
    with pytest.raises(TargetError) as error:
        connections.load()
    assert "secret" not in str(error.value)
    assert "password=secret" in connections.path.read_text(encoding="utf-8")


@pytest.mark.parametrize("execute", [engine.run_query, engine.explain_query])
@pytest.mark.parametrize("dsn", [DSN, "mysql://user:password@host/shop"])
def test_remote_execution_installs_attaches_and_binds(execute, dsn: str, scanner: MagicMock) -> None:
    execute("SELECT :n", target=dsn, params={"n": 7})
    target = parse_target(dsn)
    statements = [call.args[0] for call in scanner.execute.call_args_list]
    assert statements[:3] == [
        f"INSTALL {target.kind}", f"LOAD {target.kind}", engine._remote_attach_sql(target)
    ]
    assert statements[3].endswith("SELECT $n")
    assert scanner.execute.call_args.args[1] == {"n": 7}
    scanner.close.assert_called_once()


@pytest.mark.parametrize("method", ["execute", "fetchall", "close"])
@pytest.mark.parametrize("execute", [engine.run_query, engine.explain_query])
def test_remote_errors_never_expose_driver_text(
    method: str, execute, scanner: MagicMock
) -> None:
    getattr(scanner, method).side_effect = RuntimeError(
        f"failed with {DSN}; decoded password p@ss'word; password='private'"
    )
    with pytest.raises(engine.EngineError) as error:
        execute("SELECT 1", target=DSN)
    assert "Driver details omitted" in str(error.value)
    assert "private" not in str(error.value)
    assert "p@ss" not in str(error.value)
    assert error.value.__suppress_context__
    scanner.close.assert_called_once()


@pytest.mark.parametrize("execute", [engine.run_query, engine.explain_query])
def test_remote_requires_duckdb(execute, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "DUCKDB_AVAILABLE", False)
    with pytest.raises(engine.EngineError, match="Remote targets require DuckDB"):
        execute("SELECT 1", target=DSN)
    monkeypatch.setattr(engine, "DUCKDB_AVAILABLE", True)
    with pytest.raises(engine.EngineError, match="Remote targets require DuckDB"):
        execute("SELECT 1", target=DSN, engine="sqlite")


@pytest.mark.parametrize("command", ["run", "explain"])
def test_cli_named_connection(
    command: str, scanner: MagicMock, home: Path
) -> None:
    assert runner.invoke(app, ["add", "-n", "q", "-q", "SELECT 1"]).exit_code == 0
    added = runner.invoke(app, ["conn", "add", "prod", DSN])
    assert added.exit_code == 0, added.output
    listed = runner.invoke(app, ["conn", "ls"])
    assert listed.exit_code == 0, listed.output
    assert "prod" in listed.output
    assert "analyst" not in listed.output
    assert "private" not in listed.output
    result = runner.invoke(app, [command, "q", "--target", "@prod"])
    assert result.exit_code == 0, result.output
    assert any("READ_ONLY" in call.args[0] for call in scanner.execute.call_args_list)
    assert DSN not in (home / "pack.yaml").read_text(encoding="utf-8")
    conflict = runner.invoke(
        app, [command, "q", "--target", DSN, "--db", "other.duckdb"]
    )
    assert conflict.exit_code != 0
    assert "cannot be combined" in conflict.output
    assert DSN not in conflict.output
    assert runner.invoke(app, ["conn", "rm", "prod"]).exit_code == 0
    missing = runner.invoke(app, [command, "q", "--target", "@prod"])
    assert missing.exit_code != 0
    assert "No connection" in missing.output


@pytest.mark.parametrize("command", ["run", "explain"])
def test_cli_local_target_preserves_sqlite_behavior(command: str, home: Path) -> None:
    path = home / "sales.csv"
    path.write_text("amount\n7\n", encoding="utf-8")
    runner.invoke(app, ["add", "-n", "q", "-q", "SELECT amount FROM sales"])
    result = runner.invoke(app, [command, "q", "--target", str(path), "--engine", "sqlite"])
    assert result.exit_code == 0, result.output
    if command == "run":
        assert "7" in result.output


def test_cli_local_target_glob(home: Path) -> None:
    (home / "one.csv").write_text("n\n1\n", encoding="utf-8")
    runner.invoke(app, ["add", "-n", "q", "-q", "SELECT n FROM data"])
    result = runner.invoke(
        app, ["run", "q", "--target", str(home / "*.csv"), "--engine", "sqlite"]
    )
    assert result.exit_code == 0, result.output
    assert "1" in result.output


@pytest.mark.parametrize("command", ["run", "explain"])
def test_cli_remote_failure_is_redacted(command: str, scanner: MagicMock) -> None:
    runner.invoke(app, ["add", "-n", "q", "-q", "SELECT 1"])
    scanner.execute.side_effect = RuntimeError(f"DSN: {DSN}; p@ss'word; private")
    result = runner.invoke(app, [command, "q", "--target", DSN])
    assert result.exit_code == 1
    assert "scanner" in result.output
    assert "private" not in result.output
    assert "p@ss" not in result.output
    assert DSN not in result.output


@pytest.mark.parametrize("execute", [engine.run_query, engine.explain_query])
def test_local_database_target(execute, home: Path) -> None:
    import sqlite3

    path = home / "shop.sqlite"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE orders (id INTEGER)")
        con.execute("INSERT INTO orders VALUES (7)")
    result = execute("SELECT id FROM orders", target=str(path), engine="sqlite")
    if isinstance(result, engine.QueryResult):
        assert result.rows == [(7,)]
    else:
        assert "SCAN" in result.plan.upper()


@pytest.mark.parametrize("variable", ["QUACKPACK_TEST_POSTGRES_DSN", "QUACKPACK_TEST_MYSQL_DSN"])
def test_live_scanner_read_only(variable: str) -> None:
    dsn = os.environ.get(variable)
    if not dsn:
        pytest.skip(f"Set {variable} to opt in to a live database smoke test.")
    if not engine.DUCKDB_AVAILABLE:
        pytest.skip("DuckDB is required for live scanner tests.")
    result = engine.run_query(
        "SELECT readonly FROM duckdb_databases() WHERE database_name = 'remote'",
        target=dsn,
    )
    assert result.rows == [(True,)]
