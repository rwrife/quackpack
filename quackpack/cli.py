"""Command-line entry point for quackpack.

Wires the Typer app together. M1 shipped ``--version`` + ``hello``; M2 added the
catalog CRUD commands (``add`` / ``ls`` / ``show`` / ``rm``). M3 added ``run`` —
executing a stored query against a ``--db``/``--file`` target via DuckDB (with a
SQLite fallback). M4 makes queries reusable: ``--param`` values are type-coerced
(int/float/str) and any declared ``:param`` you omit is prompted for when
running interactively, all bound via safe prepared statements. M5 rounds out
the library workflow: ``search`` finds a query by any field, ``edit`` opens it
in ``$EDITOR`` (re-parsing ``:params`` on save), and every ``run`` records run
history so ``ls``/``show`` surface "last run Nd ago" and the last outcome.
Beyond the milestones, ``pipe`` (backlog #7) closes the capture loop: run a
throwaway query from stdin and then *offer to stash it* — nudging harder when
you've piped the same SQL before. Param presets (backlog #8) name a reusable set
of ``:param`` values per query (``preset add/ls/rm``) so ``run --preset q3-2026``
replays a canned report in one keystroke. Query templating (backlog #10) lets a
query reference another with ``{{ other_query }}``: those refs inline as
parenthesised subqueries at ``run`` time (with cycle detection), and
``show --expanded`` previews the flattened SQL — so common cleaning/joins become
reusable building blocks. Result snapshots & diff (backlog #3) cache each
successful ``run`` result and add ``diff`` — re-run a query and see what rows were
added/removed (and, with a recorded ``--key``, which rows *changed* column by
column) since last time; ``snapshot show/rm`` inspect or clear the cache.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Optional

import typer
import yaml
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from . import __version__
from .engine import EngineError, available_engines, run_query
from .history import ERROR, OK, describe_last_run, humanize_age
from .iox import (
    IMPORT_STRATEGIES,
    ImportError_,
    build_export,
    missing_names,
    parse_export,
    plan_import,
)
from .params import coerce_value, extract_params, split_param_key
from .pipes import PipeLog
from .render import FORMATS, render
from .snapshots import (
    DiffResult,
    Snapshot,
    SnapshotError,
    delete_snapshot,
    diff_results,
    load_snapshot,
    save_snapshot,
)
from .store import (
    Catalog,
    CatalogError,
    DuplicateQueryError,
    PresetError,
    PresetNotFoundError,
    Query,
    QueryNotFoundError,
    catalog_path,
)
from .templating import (
    TemplateError,
    expand_query,
    extract_refs,
)

app = typer.Typer(
    name="quackpack",
    help="A personal pantry for your SQL. Stash, tag, parameterize, and rerun your queries.",
    no_args_is_help=True,
    add_completion=False,
    # Render command help as Markdown so inline-code markup (`--file`, `:param`)
    # styles cleanly in --help instead of leaking literal backticks. Command
    # docstrings below use single-backtick Markdown for that reason.
    rich_markup_mode="markdown",
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


def _fmt_binding(binding: dict) -> str:
    """Render a preset's ``{param: value}`` mapping as ``k=v`` pairs for display.

    Keys are sorted for stable output; an empty binding shows as ``(empty)`` so
    a preset saved without values is still legible.
    """
    if not binding:
        return "(empty)"
    return ", ".join(f"{k}={binding[k]}" for k in sorted(binding))


def _cell(value: Any) -> str:
    """Stringify a diff cell (None -> dim NULL marker), matching render.py."""
    if value is None:
        return "[dim]NULL[/dim]"
    return str(value)


def _render_diff(diff: DiffResult, name: str, *, taken: str) -> None:
    """Print a :class:`~quackpack.snapshots.DiffResult` as tidy Rich tables.

    Added rows are green (``+``), removed red (``-``), and changed rows (only
    when a key was used) show each differing column as ``old → new``. A header
    line notes the snapshot age and the ``+A -R ~C`` summary so a glance answers
    "what changed since last time?".
    """
    age = humanize_age(taken)
    keynote = f" (key: {', '.join(diff.key)})" if diff.keyed and diff.key else ""
    console.print(
        f"diff [bold cyan]{name}[/bold cyan] vs snapshot from "
        f"[dim]{age}[/dim]{keynote}"
    )

    if diff.is_empty:
        console.print("[green]no changes[/green] — result is identical to the snapshot.")
        return

    cols = diff.columns or []

    if diff.added:
        table = Table(title=None, header_style="bold green", show_lines=False)
        table.add_column("+", style="green", no_wrap=True)
        for c in cols:
            table.add_column(str(c))
        for row in diff.added:
            table.add_row("+", *(_cell(row.get(c)) for c in cols))
        console.print(table)

    if diff.removed:
        table = Table(title=None, header_style="bold red", show_lines=False)
        table.add_column("-", style="red", no_wrap=True)
        for c in cols:
            table.add_column(str(c))
        for row in diff.removed:
            table.add_row("-", *(_cell(row.get(c)) for c in cols))
        console.print(table)

    if diff.changed:
        table = Table(title=None, header_style="bold yellow", show_lines=False)
        table.add_column("~", style="yellow", no_wrap=True)
        for c in diff.key:
            table.add_column(str(c), style="cyan")
        table.add_column("changes")
        for rc in diff.changed:
            deltas = ", ".join(
                f"{col}: {_cell(before)} → {_cell(after)}"
                for col, (before, after) in rc.changes.items()
            )
            keyvals = [_cell(rc.after.get(c, rc.before.get(c))) for c in diff.key]
            table.add_row("~", *keyvals, deltas)
        console.print(table)

    console.print(f"[dim]{diff.summary()}[/dim]")


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


def _resolve_params(
    declared: List[str],
    supplied: Optional[List[str]],
    *,
    base: Optional[dict] = None,
) -> dict:
    """Merge ``--param`` flags with declared ``:param`` names, prompting for gaps.

    An optional *base* mapping (e.g. a ``--preset``'s saved values) seeds the
    params first; parsed ``--param`` flags then override any overlapping keys,
    and finally for any declared placeholder *still* missing we either prompt
    (real TTY) or warn and let the engine raise a precise binding error
    (pipes/CI). Shared by ``run`` and ``pipe`` so both treat parameters
    identically.
    """
    params: dict[str, Any] = dict(base or {})
    params.update(_parse_params(supplied))
    missing = [p for p in declared if p not in params]
    if missing:
        if _stdin_is_interactive():
            params.update(_prompt_for_missing(missing))
        else:
            err_console.print(
                f"[yellow]warning:[/yellow] no value given for: {', '.join(missing)} "
                f"(pass --param {missing[0]}=... )"
            )
    return params


def _stdin_is_interactive() -> bool:
    """True when we can safely prompt (a real TTY on stdin).

    Factored out so tests can monkeypatch it, and so piped/CI invocations never
    block waiting on input that will never come.
    """
    try:
        return sys.stdin.isatty()
    except (ValueError, OSError):  # pragma: no cover - detached stdin
        return False


def _resolve_editor(explicit: Optional[str]) -> str:
    """Pick the editor command: ``--editor`` > ``$VISUAL`` > ``$EDITOR`` > default.

    Falls back to ``vi`` on POSIX and ``notepad`` on Windows so ``edit`` works
    even on a bare environment. Kept tiny and pure for easy testing.
    """
    if explicit:
        return explicit
    for var in ("VISUAL", "EDITOR"):
        val = os.environ.get(var)
        if val:
            return val
    return "notepad" if os.name == "nt" else "vi"


def _edit_text(initial: str, *, editor: Optional[str] = None, suffix: str = ".sql") -> Optional[str]:
    """Open *initial* in an editor and return the edited text (or ``None``).

    Writes *initial* to a temp file, launches the resolved editor on it, then
    reads it back. Returns ``None`` when the editor can't be launched or the
    content is byte-for-byte unchanged, signalling "no edit" to the caller.
    This is the one impure seam in ``edit``; tests monkeypatch it so they never
    spawn a real editor.
    """
    cmd = _resolve_editor(editor)
    fd, name = tempfile.mkstemp(suffix=suffix, prefix="quackpack-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(initial)
        before = Path(name).read_text(encoding="utf-8")
        try:
            # ``shell=True`` so EDITOR values with args (e.g. "code --wait") work.
            subprocess.run(f'{cmd} "{name}"', shell=True, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise _fail(f"Could not launch editor {cmd!r}: {exc}")
        after = Path(name).read_text(encoding="utf-8")
        if after == before:
            return None
        return after
    finally:
        try:
            os.unlink(name)
        except OSError:  # pragma: no cover - temp cleanup best-effort
            pass


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
    """Save a query from `-q`, a file, or stdin.

    `:param` placeholders in the SQL are detected and recorded automatically.
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
    table.add_column("last run", style="green", no_wrap=True)
    table.add_column("description")
    for q in rows:
        table.add_row(
            q.name,
            ", ".join(q.tags),
            ", ".join(q.params),
            describe_last_run(q.last_run, q.last_status),
            q.desc,
        )
    console.print(table)


@app.command()
def show(
    name: str = typer.Argument(..., help="Name of the query to display."),
    expanded: bool = typer.Option(
        False,
        "--expanded",
        help="Resolve `{{ query }}` references and show the flattened SQL.",
    ),
) -> None:
    """Print a stored query's SQL plus its metadata.

    With `--expanded`, any `{{ other_query }}` references are inlined (as
    parenthesised subqueries) into a single flat SQL string — the exact SQL
    `run` would execute — with cycles and unknown references reported as errors.
    """
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
    meta.append(f"runs: {q.run_count}")
    meta.append(f"last run: {describe_last_run(q.last_run, q.last_status)}")
    if meta:
        console.print("[dim]" + "  |  ".join(meta) + "[/dim]")
    # Surface composition so `show` documents the building blocks a query pulls
    # in (only when it actually references others).
    refs = extract_refs(q.sql)
    if refs:
        console.print(f"[dim]references: {', '.join(refs)}[/dim]")
    if q.presets:
        console.print("[bold]presets:[/bold]")
        for pname in sorted(q.presets):
            console.print(f"  [green]{pname}[/green]: {_fmt_binding(q.presets[pname])}")

    sql = q.sql
    if expanded:
        try:
            sql = expand_query(catalog, name)
        except TemplateError as exc:
            raise _fail(str(exc))
    console.print(Syntax(sql, "sql", theme="ansi_dark", word_wrap=True))


@app.command()
def search(
    text: str = typer.Argument(
        ..., help="Text to match against name, SQL, description, and tags."
    ),
) -> None:
    """Find saved queries by substring (case-insensitive) across all fields.

    Matches the same fields as the catalog's search — name, SQL body,
    description, and tags — so you can recall a query by *anything* you remember
    about it ("that one with the window function" -> `quackpack search window`).
    Results render like `ls` so you immediately see tags, params, and recency.
    """
    catalog = _load()
    rows = catalog.search(text)
    if not rows:
        console.print(f"No queries matching [bold]{text}[/bold].")
        return

    table = Table(title=None, header_style="bold", show_lines=False)
    table.add_column("name", style="bold cyan", no_wrap=True)
    table.add_column("tags", style="magenta")
    table.add_column("params", style="yellow")
    table.add_column("last run", style="green", no_wrap=True)
    table.add_column("description")
    for q in rows:
        table.add_row(
            q.name,
            ", ".join(q.tags),
            ", ".join(q.params),
            describe_last_run(q.last_run, q.last_status),
            q.desc,
        )
    console.print(table)
    plural = "match" if len(rows) == 1 else "matches"
    console.print(f"[dim]{len(rows)} {plural}[/dim]")


@app.command()
def edit(
    name: str = typer.Argument(..., help="Name of the query to edit."),
    editor: Optional[str] = typer.Option(
        None,
        "--editor",
        help="Editor command to use (defaults to $VISUAL/$EDITOR, then a sane default).",
    ),
) -> None:
    """Open a saved query's SQL in `$EDITOR` and save the edited version.

    Launches your editor (`--editor`, else `$VISUAL`/`$EDITOR`) on the
    query's current SQL. On save, the new text replaces the stored SQL and the
    `:param` list is re-derived, so adding or removing a placeholder is picked
    up automatically. Leaving the text unchanged (or emptying it) is a no-op —
    the catalog is left untouched.
    """
    catalog = _load()
    try:
        q = catalog.get(name)
    except QueryNotFoundError as exc:
        raise _fail(str(exc))

    edited = _edit_text(q.sql, editor=editor)
    if edited is None:
        # Editor exited without changes (or isn't available).
        console.print("[dim]no changes — left as-is.[/dim]")
        raise typer.Exit()

    new_sql = edited.strip()
    if not new_sql:
        raise _fail("The edited query is empty; nothing saved.")
    if new_sql == q.sql:
        console.print("[dim]no changes — left as-is.[/dim]")
        raise typer.Exit()

    old_params = list(q.params)
    q.sql = new_sql
    q.params = extract_params(new_sql)  # re-parse on save
    try:
        catalog.update(q)
    except CatalogError as exc:
        raise _fail(str(exc))

    bits = [f"[green]updated[/green] [bold cyan]{q.name}[/bold cyan]"]
    if q.params != old_params:
        bits.append(f"params: {', '.join(q.params) or '(none)'}")
    console.print("  ".join(bits))


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
    preset: Optional[str] = typer.Option(
        None,
        "--preset",
        "-P",
        help="Apply a saved preset's param values as a base (--param overrides).",
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
    key: Optional[List[str]] = typer.Option(
        None,
        "--key",
        "-k",
        help="Identity column(s) for `diff` (repeatable). Stored with the snapshot.",
    ),
    snapshot: bool = typer.Option(
        True,
        "--snapshot/--no-snapshot",
        help="Cache this result so `quackpack diff` can compare later runs.",
    ),
) -> None:
    """Run a stored query against a data target and render the results.

    The query's SQL can reference a `--file` by its auto-derived relation name
    (the file's stem, e.g. `sales.csv` -> `sales`) or via DuckDB table
    functions like `read_csv_auto('sales.csv')`. `:param` placeholders are
    bound from `--param key=value` (values are typed as int/float/str; add a
    `key:type` hint to force one). A `--preset NAME` supplies a saved set of
    param values as the base layer, so `quackpack run sales --preset q3-2026`
    reruns a canned report in one keystroke; any `--param` you pass alongside
    overrides the matching preset value. Any declared param still missing is
    prompted for interactively when running in a terminal.

    Unless `--no-snapshot` is given, the result is cached so a later
    `quackpack diff <name>` can show what changed since this run. Pass `--key
    <col>` (repeatable) to record identity columns — with a key, `diff` can
    report *changed* rows (per-column before/after), not just added/removed.
    """
    if fmt.lower() not in FORMATS:
        raise _fail(f"Unknown --format {fmt!r}. Choose one of: {', '.join(FORMATS)}.")

    catalog = _load()
    try:
        query = catalog.get(name)
    except QueryNotFoundError as exc:
        raise _fail(str(exc))

    # Composition: inline any `{{ other_query }}` references into a single flat
    # SQL string before execution. Unknown refs / cycles surface as clean
    # `error:` messages here rather than as opaque SQL failures downstream.
    try:
        sql = expand_query(catalog, name)
    except TemplateError as exc:
        raise _fail(str(exc))

    # A preset seeds a base layer of param values; explicit --param flags (and
    # any interactive prompts) win over it. Merged inside _resolve_params so the
    # "which params are still missing?" bookkeeping stays in one place.
    preset_values: Optional[dict] = None
    if preset is not None:
        try:
            preset_values = dict(query.get_preset(preset))
        except PresetNotFoundError as exc:
            raise _fail(str(exc))

    # Reconcile declared params with what was supplied. Params can come from a
    # referenced query too, so derive the expected set from the *expanded* SQL
    # rather than only the top-level query's stored list. Anything still missing
    # is prompted for when we have a real TTY; otherwise (pipes/CI) we warn and
    # let the engine raise a precise binding error if the param is truly needed.
    expected_params = extract_params(sql)
    params = _resolve_params(expected_params, param, base=preset_values)

    try:
        result = run_query(
            sql,
            db=db,
            file=file,
            params=params,
            engine=engine,
        )
    except EngineError as exc:
        # Record the failed attempt so history reflects reality, then surface
        # the error. Persisting must never mask the original failure.
        try:
            catalog.record_run(query.name, ERROR)
        except CatalogError:  # pragma: no cover - best-effort bookkeeping
            pass
        raise _fail(str(exc))

    try:
        catalog.record_run(query.name, OK)
    except CatalogError:  # pragma: no cover - best-effort bookkeeping
        pass

    # Cache the result so a later `diff` can compare against it. Best-effort:
    # a snapshot write failure must never break a successful run, so we warn
    # and carry on rather than raising.
    if snapshot:
        key_cols = [k.strip() for k in (key or []) if k and k.strip()]
        try:
            snap = Snapshot.from_result(
                query.name,
                result,
                key=key_cols,
                params=params,
                engine=engine,
            )
            save_snapshot(snap)
        except OSError as exc:  # pragma: no cover - filesystem edge
            err_console.print(
                f"[yellow]warning:[/yellow] could not save snapshot: {exc}"
            )

    render(result, fmt, console)


@app.command()
def diff(
    name: str = typer.Argument(..., help="Name of the saved query to diff against its snapshot."),
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
    preset: Optional[str] = typer.Option(
        None,
        "--preset",
        "-P",
        help="Apply a saved preset's param values as a base (--param overrides).",
    ),
    engine: str = typer.Option(
        "auto",
        "--engine",
        "-e",
        help="Execution engine: auto, duckdb, or sqlite.",
    ),
    key: Optional[List[str]] = typer.Option(
        None,
        "--key",
        "-k",
        help="Identity column(s) for the diff (repeatable). Defaults to the key saved with the snapshot.",
    ),
    update: bool = typer.Option(
        False,
        "--update/--no-update",
        help="After diffing, refresh the snapshot to the current result.",
    ),
) -> None:
    """Show what changed in a query's result since its last cached run.

    Re-runs the stored query (same targeting/params as `run`), then compares the
    fresh result against the snapshot saved on the previous run. Rows are matched
    by the snapshot's `--key` columns when it has them (so `diff` reports
    *changed* rows with per-column before/after), or by whole-row identity
    otherwise (added/removed only). Override the identity with `--key <col>`.
    Pass `--update` to also refresh the snapshot to the current result, so the
    next `diff` compares against *this* run. Distinct from BI: no dashboards,
    just "what changed since last time?".
    """
    catalog = _load()
    try:
        query = catalog.get(name)
    except QueryNotFoundError as exc:
        raise _fail(str(exc))

    # Load the baseline snapshot first: with nothing to compare against there is
    # no diff to show, so guide the user to run the query once.
    try:
        snap = load_snapshot(name)
    except SnapshotError as exc:
        raise _fail(str(exc))
    if snap is None:
        raise _fail(
            f"No snapshot for {name!r} yet. Run it once first "
            f"([bold]quackpack run {name} ...[/bold]) to cache a result to diff against."
        )

    # Expand `{{ refs }}` exactly like run so composed queries diff too.
    try:
        sql = expand_query(catalog, name)
    except TemplateError as exc:
        raise _fail(str(exc))

    preset_values: Optional[dict] = None
    if preset is not None:
        try:
            preset_values = dict(query.get_preset(preset))
        except PresetNotFoundError as exc:
            raise _fail(str(exc))

    expected_params = extract_params(sql)
    params = _resolve_params(expected_params, param, base=preset_values)

    try:
        result = run_query(sql, db=db, file=file, params=params, engine=engine)
    except EngineError as exc:
        # A diff run still counts as a run for history purposes.
        try:
            catalog.record_run(query.name, ERROR)
        except CatalogError:  # pragma: no cover - best-effort bookkeeping
            pass
        raise _fail(str(exc))

    try:
        catalog.record_run(query.name, OK)
    except CatalogError:  # pragma: no cover - best-effort bookkeeping
        pass

    # An explicit --key overrides whatever the snapshot recorded; otherwise the
    # diff uses the snapshot's stored key (possibly none -> whole-row identity).
    override_key = [k.strip() for k in (key or []) if k and k.strip()]
    diff_key = override_key or snap.key

    try:
        result_diff = diff_results(snap.as_result(), result, key=diff_key)
    except SnapshotError as exc:
        raise _fail(str(exc))

    _render_diff(result_diff, name, taken=snap.taken)

    if update:
        try:
            new_snap = Snapshot.from_result(
                query.name, result, key=diff_key, params=params, engine=engine
            )
            save_snapshot(new_snap)
            console.print("[dim]snapshot updated to current result.[/dim]")
        except OSError as exc:  # pragma: no cover - filesystem edge
            err_console.print(
                f"[yellow]warning:[/yellow] could not update snapshot: {exc}"
            )


@app.command()
def pipe(
    query: Optional[str] = typer.Option(
        None, "--query", "-q", help="The SQL to run (inline); defaults to stdin."
    ),
    file_sql: Optional[Path] = typer.Option(
        None,
        "--sql-file",
        help="Read the SQL from a file instead of stdin.",
        exists=False,
    ),
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
    save_as: Optional[str] = typer.Option(
        None,
        "--save-as",
        "-s",
        help="Stash the query under this name after running (no prompt).",
    ),
    tags: Optional[str] = typer.Option(
        None, "--tags", "-t", help="Tags to attach when saving (comma-separated)."
    ),
    desc: Optional[str] = typer.Option(
        None, "--desc", "-d", help="Description to attach when saving."
    ),
    no_save: bool = typer.Option(
        False,
        "--no-save",
        help="Run only — never prompt to stash (good for scripts).",
    ),
) -> None:
    """Run an ad-hoc query from stdin, then offer to stash it if it's a keeper.

    The fast path is a pipe: `echo "select ..." | quackpack pipe --file data.csv`
    runs the SQL immediately — no `add` first. Afterwards, if you're at a
    terminal, `pipe` asks whether to save it under a name (and nudges harder
    when you've piped the *same* query before). Non-interactively, pass
    `--save-as NAME` to stash in one shot, or `--no-save` to never be asked.

    Everything about execution — `--file`/`--db` targets, `:param` binding,
    `--format`, `--engine` — matches `run`; `pipe` just sources the SQL
    from stdin/`-q`/`--sql-file` instead of a saved name.
    """
    if fmt.lower() not in FORMATS:
        raise _fail(f"Unknown --format {fmt!r}. Choose one of: {', '.join(FORMATS)}.")

    sql = _read_sql(query, file_sql)
    params = _resolve_params(extract_params(sql), param)

    try:
        result = run_query(sql, db=db, file=file, params=params, engine=engine)
    except EngineError as exc:
        raise _fail(str(exc))

    render(result, fmt, console)

    # Remember this pipe so future runs of the same query can nudge harder. This
    # is best-effort UX: a logging hiccup must never sink a successful run.
    seen_before = 0
    try:
        log = PipeLog.load()
        seen_before = log.count_for(sql)  # times piped *before* this run
        log.record(sql)
        log.save()
    except OSError:  # pragma: no cover - sidecar is best-effort
        pass

    _maybe_stash(sql, save_as=save_as, tags=tags, desc=desc, no_save=no_save, seen_before=seen_before)


def _maybe_stash(
    sql: str,
    *,
    save_as: Optional[str],
    tags: Optional[str],
    desc: Optional[str],
    no_save: bool,
    seen_before: int,
) -> None:
    """Save *sql* to the catalog, by flag or interactive prompt.

    Decision order:

    * ``--no-save`` → do nothing (but still hint how to stash if it's a repeat).
    * ``--save-as NAME`` → stash immediately under NAME (non-interactive path).
    * a real TTY → prompt for a name (blank skips); a query piped before makes
      the prompt a stronger nudge.
    * otherwise (piped/CI, no flag) → print a one-line hint and move on, so the
      run never blocks waiting on input that can't arrive.
    """
    if no_save:
        if seen_before:
            console.print(
                f"[dim]tip: you've piped this {seen_before + 1}× — "
                f"stash it with [bold]--save-as NAME[/bold].[/dim]"
            )
        return

    name: Optional[str] = None
    if save_as is not None:
        name = save_as.strip()
        if not name:
            raise _fail("--save-as needs a non-empty name.")
    elif _stdin_is_interactive():
        if seen_before:
            console.print(
                f"[yellow]✨ you've piped this {seen_before + 1}× already — "
                f"worth stashing?[/yellow]"
            )
        answer = typer.prompt(
            "stash as [name] (blank to skip)", default="", show_default=False
        )
        name = answer.strip()
        if not name:
            console.print("[dim]not stashed.[/dim]")
            return
    else:
        # Non-interactive and no --save-as: don't block, just hint.
        if seen_before:
            console.print(
                f"[dim]tip: you've piped this {seen_before + 1}× — "
                f"stash it with [bold]--save-as NAME[/bold].[/dim]"
            )
        return

    record = Query(name=name, sql=sql, tags=_parse_tags(tags), desc=desc or "")
    catalog = _load()
    try:
        catalog.add(record)
    except DuplicateQueryError:
        raise _fail(
            f"A query named {record.name!r} already exists. "
            f"Pick another name or use [bold]quackpack add --overwrite[/bold]."
        )
    except CatalogError as exc:
        raise _fail(str(exc))

    bits = [f"[green]stashed[/green] [bold cyan]{record.name}[/bold cyan]"]
    if record.tags:
        bits.append(f"tags: {', '.join(record.tags)}")
    if record.params:
        bits.append(f"params: {', '.join(record.params)}")
    console.print("  ".join(bits))


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


# --------------------------------------------------------------------------
# Command group: preset add / ls / rm  (backlog #8)
# --------------------------------------------------------------------------

preset_app = typer.Typer(
    name="preset",
    help="Manage saved param presets (named sets of :param values) on a query.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="markdown",
)
app.add_typer(preset_app, name="preset")


@preset_app.command("add")
def preset_add(
    query: str = typer.Argument(..., help="Name of the saved query to attach the preset to."),
    preset: str = typer.Argument(..., help="Name for this preset, e.g. `q3-2026`."),
    param: Optional[List[str]] = typer.Option(
        None,
        "--param",
        "-p",
        help="A :param value as key=value (repeatable).",
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Replace an existing preset with the same name."
    ),
) -> None:
    """Save a named set of `:param` values on a query.

    Bundles the `--param key=value` pairs (typed just like `run`, with optional
    `key:type` hints) under `preset` so `quackpack run <query> --preset <name>`
    reruns it in one keystroke. Params that aren't declared by the query are
    still stored, but flagged with a warning so a typo is easy to catch.
    """
    catalog = _load()
    try:
        q = catalog.get(query)
    except QueryNotFoundError as exc:
        raise _fail(str(exc))

    pname = preset.strip()
    if not pname:
        raise _fail("The preset name must not be empty.")
    if pname in q.presets and not overwrite:
        raise _fail(
            f"Query {q.name!r} already has a preset named {pname!r}. "
            f"Re-run with [bold]--overwrite[/bold] to replace it."
        )

    values = _parse_params(param)
    if not values:
        raise _fail("A preset needs at least one --param key=value.")

    unknown = [k for k in values if k not in q.params]
    if unknown:
        err_console.print(
            f"[yellow]warning:[/yellow] preset sets param(s) not used by "
            f"{q.name!r}: {', '.join(unknown)}"
        )

    try:
        catalog.set_preset(q.name, pname, values)
    except (PresetError, CatalogError) as exc:
        raise _fail(str(exc))

    console.print(
        f"[green]saved preset[/green] [green]{pname}[/green] on "
        f"[bold cyan]{q.name}[/bold cyan]  {_fmt_binding(values)}"
    )


@preset_app.command("ls")
def preset_ls(
    query: str = typer.Argument(..., help="Name of the saved query whose presets to list."),
) -> None:
    """List the presets saved on a query."""
    catalog = _load()
    try:
        q = catalog.get(query)
    except QueryNotFoundError as exc:
        raise _fail(str(exc))

    if not q.presets:
        console.print(
            f"No presets on [bold cyan]{q.name}[/bold cyan]. "
            f"Add one with [bold]quackpack preset add {q.name} <name> -p k=v[/bold]."
        )
        return

    table = Table(title=None, header_style="bold", show_lines=False)
    table.add_column("preset", style="green", no_wrap=True)
    table.add_column("bindings")
    for pname in sorted(q.presets):
        table.add_row(pname, _fmt_binding(q.presets[pname]))
    console.print(table)
    plural = "preset" if len(q.presets) == 1 else "presets"
    console.print(f"[dim]{len(q.presets)} {plural} on {q.name}[/dim]")


@preset_app.command("rm")
def preset_rm(
    query: str = typer.Argument(..., help="Name of the saved query."),
    preset: str = typer.Argument(..., help="Name of the preset to remove."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Remove a saved preset from a query."""
    catalog = _load()
    try:
        q = catalog.get(query)
    except QueryNotFoundError as exc:
        raise _fail(str(exc))
    try:
        q.get_preset(preset)
    except PresetNotFoundError as exc:
        raise _fail(str(exc))

    if not yes:
        confirm = typer.confirm(f"Remove preset {preset!r} from {q.name!r}?")
        if not confirm:
            console.print("Aborted.")
            raise typer.Exit()

    try:
        catalog.remove_preset(q.name, preset)
    except (PresetNotFoundError, CatalogError) as exc:
        raise _fail(str(exc))
    console.print(
        f"[green]removed preset[/green] [green]{preset.strip()}[/green] from "
        f"[bold cyan]{q.name}[/bold cyan]"
    )


snapshot_app = typer.Typer(
    name="snapshot",
    help="Inspect or clear the cached result a query's `diff` compares against.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="markdown",
)
app.add_typer(snapshot_app, name="snapshot")


@snapshot_app.command("show")
def snapshot_show(
    name: str = typer.Argument(..., help="Name of the saved query whose snapshot to show."),
    fmt: str = typer.Option(
        "table",
        "--format",
        "-F",
        help=f"Output format: {', '.join(FORMATS)}.",
    ),
) -> None:
    """Show the cached result (and its metadata) saved for a query.

    Prints when the snapshot was taken, the identity `--key` recorded (if any),
    the params it ran with, and the cached rows themselves — handy for seeing
    exactly what the next `quackpack diff` will compare against.
    """
    if fmt.lower() not in FORMATS:
        raise _fail(f"Unknown --format {fmt!r}. Choose one of: {', '.join(FORMATS)}.")

    # Confirm the query exists so a typo'd name is a clean error, not "no snapshot".
    catalog = _load()
    try:
        catalog.get(name)
    except QueryNotFoundError as exc:
        raise _fail(str(exc))

    try:
        snap = load_snapshot(name)
    except SnapshotError as exc:
        raise _fail(str(exc))
    if snap is None:
        raise _fail(
            f"No snapshot for {name!r} yet. Run it once "
            f"([bold]quackpack run {name} ...[/bold]) to cache a result."
        )

    if fmt.lower() == "table":
        age = humanize_age(snap.taken)
        keynote = ", ".join(snap.key) if snap.key else "(whole row)"
        console.print(
            f"snapshot [bold cyan]{name}[/bold cyan]  "
            f"[dim]taken {age}· key: {keynote}· {snap.rowcount} "
            f"{'row' if snap.rowcount == 1 else 'rows'}[/dim]"
        )
        if snap.params:
            console.print(f"[dim]params: {_fmt_binding(snap.params)}[/dim]")
    render(snap.as_result(), fmt, console)


@snapshot_app.command("rm")
def snapshot_rm(
    name: str = typer.Argument(..., help="Name of the saved query whose snapshot to clear."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Delete a query's cached snapshot (the next `run` re-seeds it)."""
    try:
        snap = load_snapshot(name)
    except SnapshotError:
        # A corrupt file is still deletable; treat it as present.
        snap = Snapshot(query=name)
    if snap is None:
        console.print(f"No snapshot to remove for [bold cyan]{name}[/bold cyan].")
        return

    if not yes:
        confirm = typer.confirm(f"Remove cached snapshot for {name!r}?")
        if not confirm:
            console.print("Aborted.")
            raise typer.Exit()

    removed = delete_snapshot(name)
    if removed:
        console.print(
            f"[green]removed snapshot[/green] for [bold cyan]{name}[/bold cyan]"
        )
    else:  # pragma: no cover - race: file vanished between load and delete
        console.print(f"No snapshot to remove for [bold cyan]{name}[/bold cyan].")


# --------------------------------------------------------------------------
# Command: export / import  (backlog #5)
# --------------------------------------------------------------------------


def _dump_pack(document: dict) -> str:
    """Serialise an export *document* to YAML matching the catalog's own style."""
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100)


@app.command("export")
def export_cmd(
    names: Optional[List[str]] = typer.Argument(
        None,
        metavar="[NAMES]...",
        help="Only export these queries (by name). Omit to export the whole pack.",
    ),
    tag: Optional[str] = typer.Option(
        None,
        "--tag",
        "-t",
        help="Only export queries carrying this tag (combines with NAMES).",
    ),
    out: Optional[Path] = typer.Option(
        None,
        "--out",
        "-o",
        help="Write to this file instead of stdout.",
    ),
) -> None:
    """Export selected queries to a standalone, sharable pack file.

    Writes the chosen queries — **plus their presets and metadata, but not run
    history or cached snapshots** — as a valid pack document. With no `NAMES`
    and no `--tag` the whole pack is exported; otherwise the name arguments and
    `--tag` filter combine (AND). Output goes to stdout by default so it pipes
    straight to a file or a gist; use `-o FILE` to write it directly.

    Because a `{{ ref }}` only survives a round trip if the referenced query is
    also included, `export` warns (on stderr, non-fatally) when a selected query
    references one you left out. Exporting an empty selection is still success.
    """
    catalog = _load()

    # A name that doesn't exist is almost always a typo — fail loudly rather
    # than silently export nothing for it.
    if names:
        unknown = missing_names(catalog, names)
        if unknown:
            raise _fail(f"No query named: {', '.join(unknown)}.")

    selection = build_export(catalog, names, tag=tag)

    # Warn (non-fatal) about templating refs that point outside the selection;
    # the import on the other end would not be able to resolve them.
    for qname in sorted(selection.dangling):
        missing = ", ".join(selection.dangling[qname])
        err_console.print(
            f"[yellow]warning:[/yellow] {qname!r} references query(s) not in the "
            f"export: {missing}"
        )

    text = _dump_pack(selection.document)

    if out is not None:
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise _fail(f"Could not write {out}: {exc}")
        plural = "query" if selection.count == 1 else "queries"
        err_console.print(
            f"[green]exported[/green] {selection.count} {plural} to "
            f"[bold cyan]{out}[/bold cyan]"
        )
    else:
        # The document itself is the only thing on stdout so it pipes cleanly;
        # any human-facing note goes to stderr.
        sys.stdout.write(text)


@app.command("import")
def import_cmd(
    file: Path = typer.Argument(
        ...,
        help="An exported pack file to merge in (use `-` to read stdin).",
    ),
    strategy: str = typer.Option(
        "skip",
        "--strategy",
        "-s",
        help=f"On a name collision: {', '.join(IMPORT_STRATEGIES)} (default skip).",
    ),
    tag: Optional[str] = typer.Option(
        None,
        "--tag",
        "-t",
        help="Stamp this extra tag on every imported query (provenance).",
    ),
) -> None:
    """Merge queries from an exported pack into your pack.

    Reads a file written by `quackpack export` (or a whole `pack.yaml`; pass `-`
    to read stdin) and merges its queries + presets into your library. The
    default `--strategy skip` **never overwrites**: an incoming query whose name
    already exists is left alone. `--strategy overwrite` replaces same-name
    queries; `--strategy rename` imports collisions under a suffixed name
    (`report-2`). `--tag from-alice` appends that tag to everything imported so
    you can tell where a query came from. Prints an
    `imported / skipped / renamed` summary.
    """
    if strategy not in IMPORT_STRATEGIES:
        raise _fail(
            f"Unknown --strategy {strategy!r}. "
            f"Choose one of: {', '.join(IMPORT_STRATEGIES)}."
        )

    # Read the raw document: '-' means stdin, otherwise a file on disk.
    if str(file) == "-":
        raw_text = sys.stdin.read()
        source = "stdin"
    else:
        try:
            raw_text = file.read_text(encoding="utf-8")
        except OSError as exc:
            raise _fail(f"Could not read {file}: {exc}")
        source = str(file)

    try:
        loaded = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise _fail(f"Malformed {source}: not valid YAML ({exc}).")

    try:
        incoming = parse_export(loaded, source=source)
    except ImportError_ as exc:
        raise _fail(str(exc))

    catalog = _load()
    plan = plan_import(catalog.names(), incoming, strategy=strategy, tag=tag)

    # Apply the plan. Only the queries in `to_add` mutate the catalog; skipped
    # names and the (unchanged) existing side are left as-is. We save once at
    # the end so a big import is a single atomic write.
    for q in plan.to_add:
        catalog.add(q, overwrite=(q.name in plan.overwrite), save=False)
    if plan.to_add:
        catalog.save()

    # Report. Renames/overwrites get an explicit line so the merge is auditable.
    for original, new in sorted(plan.renamed.items()):
        console.print(
            f"[yellow]renamed[/yellow] {original} -> [bold cyan]{new}[/bold cyan] "
            f"(name already existed)"
        )
    if plan.overwrite:
        for name in sorted(plan.overwrite):
            console.print(f"[yellow]overwrote[/yellow] [bold cyan]{name}[/bold cyan]")
    if plan.skipped:
        console.print(
            f"[dim]skipped (name exists): {', '.join(sorted(plan.skipped))}[/dim]"
        )
    console.print(f"[green]{plan.summary()}[/green]")


if __name__ == "__main__":  # pragma: no cover
    app()
