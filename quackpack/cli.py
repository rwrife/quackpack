"""Command-line entry point for quackpack.

Wires the Typer app together. M1 shipped ``--version`` + ``hello``; M2 added the
catalog CRUD commands (``add`` / ``ls`` / ``show`` / ``rm``). M3 added ``run`` —
executing a stored query against a ``--db``/``--file`` target via DuckDB (with a
SQLite fallback). M4 makes queries reusable: ``--param`` values are type-coerced
(int/float/str) and any declared ``:param`` you omit is prompted for when
running interactively, all bound via safe prepared statements.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List, Optional

import typer
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from . import __version__
from .engine import EngineError, available_engines, run_query
from .params import coerce_value, split_param_key
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
    """Parse repeated ``--param key=value`` flags into a typed dict.

    Values are coerced to ``int``/``float``/``str`` (see
    :func:`quackpack.params.coerce_value`) so numeric filters compare
    numerically instead of lexically. An optional ``key:type`` annotation forces
    a specific type, e.g. ``--param n:int=5`` or ``--param zip:str=00501``. A
    missing ``=`` (or a bad explicit cast) is a user error, not a silent no-op.
    """
    out: dict[str, Any] = {}
    for item in pairs or []:
        if "=" not in item:
            raise _fail(f"Bad --param {item!r}; expected key=value.")
        raw_key, _, value = item.partition("=")
        key, type_hint = split_param_key(raw_key)
        if not key:
            raise _fail(f"Bad --param {item!r}; the key is empty.")
        try:
            out[key] = coerce_value(value, type_hint)
        except ValueError:
            raise _fail(
                f"Bad --param {item!r}: {value!r} is not a valid {type_hint}."
            )
    return out


def _prompt_for_missing(missing: List[str]) -> dict:
    """Interactively prompt for each missing param and coerce the answers.

    Each entered value runs through :func:`coerce_value` so ``25`` becomes an
    ``int`` and ``2.5`` a ``float`` — matching how ``--param`` values are typed.
    An empty answer is accepted as an empty string (the user can wrap a literal
    in the query if they need NULL semantics).
    """
    out: dict[str, Any] = {}
    for name in missing:
        raw = typer.prompt(f"param {name}")
        out[name] = coerce_value(raw)
    return out


def _stdin_is_interactive() -> bool:
    """True when we can safely prompt (a real TTY on stdin).

    Factored out so tests can monkeypatch it, and so piped/CI invocations never
    block waiting on input that will never come.
    """
    try:
        return sys.stdin.isatty()
    except (ValueError, OSError):  # pragma: no cover - detached stdin
        return False


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
    bound from ``--param key=value`` (values are typed as int/float/str; add a
    ``key:type`` hint to force one). Any declared param you don't pass is
    prompted for interactively when running in a terminal.
    """
    if fmt.lower() not in FORMATS:
        raise _fail(f"Unknown --format {fmt!r}. Choose one of: {', '.join(FORMATS)}.")

    catalog = _load()
    try:
        query = catalog.get(name)
    except QueryNotFoundError as exc:
        raise _fail(str(exc))

    params = _parse_params(param)

    # Reconcile declared params with what was supplied. Anything still missing
    # is prompted for when we have a real TTY; otherwise (pipes/CI) we warn and
    # let the engine raise a precise binding error if the param is truly needed.
    missing = [p for p in query.params if p not in params]
    if missing:
        if _stdin_is_interactive():
            params.update(_prompt_for_missing(missing))
        else:
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
