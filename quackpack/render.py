"""Result rendering for quackpack.

Turns an :class:`~quackpack.engine.QueryResult` into output the user asked for:

* ``table`` (default) — a Rich table for the terminal.
* ``csv`` — RFC-4180-ish CSV, perfect for piping into another tool.
* ``json`` — a JSON array of row objects (column-keyed), for scripts/jq.

Kept separate from the engine so output strategy and execution stay decoupled.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Iterable

from rich.console import Console
from rich.table import Table

from .engine import QueryResult

__all__ = ["FORMATS", "render", "render_table", "render_csv", "render_json"]

FORMATS = ("table", "csv", "json")


def _stringify(value: Any) -> str:
    """Render a single cell for the Rich table (None -> dim NULL marker)."""
    if value is None:
        return "[dim]NULL[/dim]"
    return str(value)


def render_table(result: QueryResult, console: Console) -> None:
    """Print *result* as a Rich table to *console*."""
    table = Table(header_style="bold", show_lines=False)
    if result.columns:
        for col in result.columns:
            table.add_column(str(col))
    else:  # pragma: no cover - queries normally return at least one column
        table.add_column("result")

    for row in result.rows:
        table.add_row(*(_stringify(v) for v in row))

    console.print(table)
    # A small footer so the user knows row counts without re-counting by eye.
    plural = "row" if result.rowcount == 1 else "rows"
    console.print(f"[dim]{result.rowcount} {plural}[/dim]")


def render_csv(result: QueryResult) -> str:
    """Return *result* serialised as CSV text (with a header row)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    if result.columns:
        writer.writerow(result.columns)
    for row in result.rows:
        writer.writerow(["" if v is None else v for v in row])
    return buf.getvalue()


def _json_default(value: Any) -> Any:
    """Fallback serialiser for types json doesn't handle natively."""
    # bytes -> utf-8 (best effort), everything else -> str.
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    return str(value)


def render_json(result: QueryResult) -> str:
    """Return *result* as a JSON array of column-keyed row objects."""
    records: list[dict[str, Any]] = [
        dict(zip(result.columns, row)) for row in result.rows
    ]
    return json.dumps(records, default=_json_default, ensure_ascii=False, indent=2)


def render(result: QueryResult, fmt: str, console: Console) -> None:
    """Render *result* in *fmt*, printing to *console*.

    ``table`` uses Rich directly; ``csv``/``json`` are emitted as plain text so
    they pipe cleanly (no Rich markup, no extra wrapping).
    """
    fmt = (fmt or "table").lower()
    if fmt == "table":
        render_table(result, console)
    elif fmt == "csv":
        # ``end=""`` because render_csv already ends with a trailing newline.
        console.print(render_csv(result), end="", markup=False, highlight=False)
    elif fmt == "json":
        console.print(render_json(result), markup=False, highlight=False)
    else:  # pragma: no cover - guarded by the CLI's choice validation
        raise ValueError(f"Unknown format {fmt!r}. Choose one of: {', '.join(FORMATS)}.")
