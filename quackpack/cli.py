"""Command-line entry point for quackpack.

Wires the Typer app together. M1 shipped ``--version`` + ``hello``; M2 added the
catalog CRUD commands (``add`` / ``ls`` / ``show`` / ``rm``). M3 adds ``run`` —
executing a stored query against a ``--db``/``--file`` target via DuckDB (with a
SQLite fallback). Parameter prompting lands in M4.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from . import __version__
from .engine import EngineError, available_engines, run_query
from .render import FORMATS, render
from .store import (
    Catalog,
    CatalogError,
    DuplicateQueryError,
    Query,
    QueryNotFoundError,
    catalog_path,
)

app = typer.Typer(
    name="quackpack",
    help="A personal pantry for your SQL. Stash, tag, parameterize, and rerun your queries.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        # Plain print so output is clean and pipe-friendly for scripts/tests.
        typer.echo(f"quackpack {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-V",
        help="Show the quackpack version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """quackpack — save the query, rerun the query."""


@app.command()
def hello(
    name: str = typer.Option(
        "world",
        "--name",
        "-n",
        help="Who to greet.",
    ),
) -> None:
    """Smoke-test command: prints a friendly duck greeting."""
    console.print(f"🦆📦 quackpack says hello, [bold cyan]{name}[/bold cyan]!")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _load() -> Catalog:
    """Load the catalog, surfacing storage errors as clean CLI failures."""
    try:
        return Catalog.load()
    except CatalogError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1)


def _fail(message: str) -> "typer.Exit":
    """Print *message* to stderr and return an Exit(1) to raise."""
    err_console.print(f"[red]error:[/red] {message}")
    return typer.Exit(code=1)


def _parse_tags(tags: Optional[str]) -> List[str]:
    """Split a comma-separated ``--tags`` value into a clean list."""
    if not tags:
        return []
    return [t.strip() for t in tags.split(",") if t.strip()]


def _read_sql(query: Optional[str], file: Optional[Path]) -> str:
    """Resolve the query text from ``-q``, a file, or stdin (in that order)."""
    if query is not None and file is not None:
        raise _fail("Pass at most one of --query/-q or --file/-f.")
    if query is not None:
        text = query
    elif file is not None:
        try:
            text = file.read_text(encoding="utf-8")
        except OSError as exc:
            raise _fail(f"Could not read {file}: {exc}")
    else:
        if sys.stdin.isatty():
            raise _fail("No query given. Use --query, --file, or pipe SQL on stdin.")
        text = sys.stdin.read()
    text = text.strip()
    if not text:
        raise _fail("The query is empty.")
    return text


def _parse_params(pairs: Optional[List[str]]) -> dict:
    """Parse repeated ``--param key=value`` flags into a dict.

    Values are kept as strings here; native driver binding handles coercion for
    the common cases, and rich typing/prompts arrive in M4. A missing ``=`` is a
    user error rather than a silent no-op.
    """
    out: dict[str, str] = {}
    for item in pairs or []:
        if "=" not in item:
            raise _fail(f"Bad --param {item!r}; expected key=value.")
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            raise _fail(f"Bad --param {item!r}; the key is empty.")
        out[key] = value
    return out


# --------------------------------------------------------------------------
# Commands: add / ls / show / run / rm
# --------------------------------------------------------------------------


@app.command()
def add(
    query: Optional[str] = typer.Option(
        None, "--query", "-q", help="The SQL to save (inline)."
    ),
    file: Optional[Path] = typer.Option(
        None, "--file", "-f", help="Read the SQL from a file.", exists=False
    ),
    name: str = typer.Option(..., "--name", "-n", help="Name to save the query under."),
    tags: Optional[str] = typer.Option(
        None, "--tags", "-t", help="Comma-separated tags, e.g. sales,adhoc."
    ),
    desc: Optional[str] = typer.Option(
        None, "--desc", "-d", help="Short human description."
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Replace an existing query with the same name."
    ),
) -> None:
    """Save a query from ``-q``, a file, or stdin.

    ``:param`` placeholders in the SQL are detected and recorded automatically.
    """
    sql = _read_sql(query, file)
    record = Query(name=name, sql=sql, tags=_parse_tags(tags), desc=desc or "")
    catalog = _load()
    try:
        catalog.add(record, overwrite=overwrite)
    except DuplicateQueryError as exc:
        raise _fail(f"{exc}\nRe-run with --overwrite to replace it.")
    except CatalogError as exc:
        raise _fail(str(exc))

    bits = [f"[green]saved[/green] [bold cyan]{record.name}[/bold cyan]"]
    if record.tags:
        bits.append(f"tags: {', '.join(record.tags)}")
    if record.params:
        bits.append(f"params: {', '.join(record.params)}")
    console.print("  ".join(bits))


@app.command("ls")
def ls(
    tag: Optional[str] = typer.Option(
        None, "--tag", "-t", help="Only show queries carrying this tag."
    ),
) -> None:
    """List saved queries (name, tags, params, description)."""
    catalog = _load()
    rows = catalog.list(tag=tag)
    if not rows:
        if tag:
            console.print(f"No queries tagged [bold]{tag}[/bold] yet.")
        else:
            console.print(
                f"No queries yet. Add one with [bold]quackpack add[/bold]. "
                f"(catalog: {catalog_path()})"
            )
        return

    table = Table(title=None, header_style="bold", show_lines=False)
    table.add_column("name", style="bold cyan", no_wrap=True)
    table.add_column("tags", style="magenta")
    table.add_column("params", style="yellow")
    table.add_column("description")
    for q in rows:
        table.add_row(
            q.name,
            ", ".join(q.tags),
            ", ".join(q.params),
            q.desc,
        )
    console.print(table)


@app.command()
def show(
    name: str = typer.Argument(..., help="Name of the query to display."),
) -> None:
    """Print a stored query's SQL plus its metadata."""
    catalog = _load()
    try:
        q = catalog.get(name)
    except QueryNotFoundError as exc:
        raise _fail(str(exc))

    console.print(f"[bold cyan]{q.name}[/bold cyan]")
    if q.desc:
        console.print(q.desc)
    meta = []
    if q.tags:
        meta.append(f"tags: {', '.join(q.tags)}")
    if q.params:
        meta.append(f"params: {', '.join(q.params)}")
    if q.created:
        meta.append(f"created: {q.created}")
    if meta:
        console.print("[dim]" + "  |  ".join(meta) + "[/dim]")
    console.print(Syntax(q.sql, "sql", theme="ansi_dark", word_wrap=True))


@app.command()
def run(
    name: str = typer.Argument(..., help="Name of the saved query to execute."),
    db: Optional[Path] = typer.Option(
        None, "--db", help="Database file to query (DuckDB or SQLite)."
    ),
    file: Optional[Path] = typer.Option(
        None,
        "--file",
        "-f",
        help="Data file to expose to the query (CSV / Parquet / SQLite).",
    ),
    param: Optional[List[str]] = typer.Option(
        None,
        "--param",
        "-p",
        help="Bind a :param as key=value (repeatable).",
    ),
    fmt: str = typer.Option(
        "table",
        "--format",
        "-F",
        help=f"Output format: {', '.join(FORMATS)}.",
    ),
    engine: str = typer.Option(
        "auto",
        "--engine",
        "-e",
        help="Execution engine: auto, duckdb, or sqlite.",
    ),
) -> None:
    """Run a stored query against a data target and render the results.

    The query's SQL can reference a ``--file`` by its auto-derived relation name
    (the file's stem, e.g. ``sales.csv`` -> ``sales``) or via DuckDB table
    functions like ``read_csv_auto('sales.csv')``. ``:param`` placeholders are
    bound from ``--param key=value`` (interactive prompting arrives in M4).
    """
    if fmt.lower() not in FORMATS:
        raise _fail(f"Unknown --format {fmt!r}. Choose one of: {', '.join(FORMATS)}.")

    catalog = _load()
    try:
        query = catalog.get(name)
    except QueryNotFoundError as exc:
        raise _fail(str(exc))

    params = _parse_params(param)

    # Warn (don't block) when the query expects params the caller didn't supply;
    # the engine will surface a precise binding error if they're truly required.
    missing = [p for p in query.params if p not in params]
    if missing:
        err_console.print(
            f"[yellow]warning:[/yellow] no value given for: {', '.join(missing)} "
            f"(pass --param {missing[0]}=... )"
        )

    try:
        result = run_query(
            query.sql,
            db=db,
            file=file,
            params=params,
            engine=engine,
        )
    except EngineError as exc:
        raise _fail(str(exc))

    render(result, fmt, console)


@app.command()
def rm(
    name: str = typer.Argument(..., help="Name of the query to remove."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Remove a saved query."""
    catalog = _load()
    try:
        target = catalog.get(name)
    except QueryNotFoundError as exc:
        raise _fail(str(exc))

    if not yes:
        confirm = typer.confirm(f"Remove query {target.name!r}?")
        if not confirm:
            console.print("Aborted.")
            raise typer.Exit()

    catalog.remove(target.name)
    console.print(f"[green]removed[/green] [bold cyan]{target.name}[/bold cyan]")


if __name__ == "__main__":  # pragma: no cover
    app()
