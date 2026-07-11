#!/usr/bin/env python3
"""Illustrative MCP shim for quackpack — expose your pack as callable tools.

**This is a demonstration, not a runtime dependency of quackpack.** It shows how
thin the glue is between quackpack's stable JSON surface (issue #26) and an MCP
server: quackpack stays a local, single-file CLI, and this ~90-line script wraps
its `tools --format jsonschema` manifest + non-interactive `run` contract as MCP
tools.

It talks to quackpack purely over the documented CLI contract:

* discovery  ->  ``quackpack tools --format jsonschema``
* invocation ->  ``quackpack run <name> --format json --no-input --param k=v ...``
                 (emits the ``{"columns": [...], "rows": [...], "rowcount": N}``
                 envelope on stdout; missing required params exit 1 with an
                 ``error:`` on stderr)

Requires the ``mcp`` package to actually serve (``pip install mcp``); without it
you can still run this file directly to print the tools it *would* expose:

    python examples/mcp_shim.py --dry-run

Nothing here reaches into quackpack's internals — swap the ``QUACKPACK`` command
for a pinned path or a container invocation and it keeps working.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any

# The quackpack executable to shell out to. Override with $QUACKPACK_BIN so this
# works against a venv, a pinned path, or a container wrapper.
QUACKPACK = os.environ.get("QUACKPACK_BIN", shutil.which("quackpack") or "quackpack")


def _run_quackpack(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Invoke quackpack with *args*, capturing stdout/stderr as text."""
    return subprocess.run(
        [QUACKPACK, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def list_tools() -> list[dict[str, Any]]:
    """Discover the pack's queries as MCP-style tool definitions."""
    proc = _run_quackpack(["tools", "--format", "jsonschema"])
    if proc.returncode != 0:
        raise RuntimeError(f"quackpack tools failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout or "[]")


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke one saved query non-interactively and return its result envelope.

    *arguments* is a ``{param: value}`` mapping (as an MCP client would pass);
    each becomes a ``--param key=value`` flag. Values are stringified because the
    run engine re-coerces them per the manifest's declared types.
    """
    args = ["run", name, "--format", "json", "--no-input"]
    for key, value in (arguments or {}).items():
        args += ["--param", f"{key}={value}"]
    proc = _run_quackpack(args)
    if proc.returncode != 0:
        # quackpack prints a clean ``error:`` line on stderr; surface it.
        raise RuntimeError(proc.stderr.strip() or "quackpack run failed")
    return json.loads(proc.stdout or "{}")


def _serve() -> None:  # pragma: no cover - requires the optional ``mcp`` package
    """Serve the pack over MCP (stdio). Requires ``pip install mcp``."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        sys.exit(
            "The 'mcp' package is required to serve. Install it with "
            "`pip install mcp`, or run with --dry-run to preview the tools."
        )

    server = FastMCP("quackpack")

    # Register each saved query as a distinct MCP tool. We close over the tool
    # name so each callable invokes the right query.
    for tool in list_tools():
        tname = tool["name"]

        def _make(tool_name: str):
            def _handler(**kwargs: Any) -> dict[str, Any]:
                return call_tool(tool_name, kwargs)

            return _handler

        server.add_tool(
            _make(tname),
            name=tname,
            description=tool.get("description") or f"quackpack query {tname!r}",
        )

    server.run()


def _dry_run() -> None:
    """Print the tools this shim would expose (no ``mcp`` dependency needed)."""
    tools = list_tools()
    if not tools:
        print("No queries in the pack yet — add some with `quackpack add`.")
        return
    print(f"Would expose {len(tools)} quackpack quer(y/ies) as MCP tools:\n")
    for tool in tools:
        required = tool["inputSchema"].get("required", [])
        props = tool["inputSchema"].get("properties", {})
        params = ", ".join(
            f"{p}:{props[p].get('type', 'string')}"
            + ("" if p in required else "?")
            for p in props
        )
        desc = tool.get("description") or "(no description)"
        print(f"  • {tool['name']}({params}) — {desc}")


if __name__ == "__main__":
    if "--dry-run" in sys.argv[1:]:
        _dry_run()
    else:
        _serve()
