"""Command-line entry point for quackpack.

M1 scaffold: only ``--version`` and ``hello`` are wired up. Real commands
(``add``/``ls``/``run``/...) land in later milestones.
"""

from __future__ import annotations

from typing import Optional

import typer
from rich.console import Console

from . import __version__

app = typer.Typer(
    name="quackpack",
    help="A personal pantry for your SQL. Stash, tag, parameterize, and rerun your queries.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()


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


if __name__ == "__main__":  # pragma: no cover
    app()
