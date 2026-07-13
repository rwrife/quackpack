"""Lightweight static SQL lints for ``quackpack explain``.

These are intentionally cheap, regex-level heuristics — not a real SQL parser.
The goal is a fast "is this query obviously dumb?" signal before you commit a
query to your library: catch broad ``SELECT *`` scans and unfiltered reads of
potentially large files. Every lint is *advisory* and non-fatal; the CLI prints
them to stderr and still shows the plan.

Each lint returns a short, actionable message. Keeping them here (pure, no I/O)
makes them trivial to unit-test and to extend without touching the engine.
"""

from __future__ import annotations

import re
from typing import List

__all__ = ["lint_sql"]

# ``SELECT *`` (optionally ``SELECT DISTINCT *`` or qualified ``t.*``). We match
# a star that is the entire projection list, which is the case worth warning on
# (``count(*)`` and ``select a, * `` style edge cases are deliberately ignored
# to keep false positives low).
_SELECT_STAR = re.compile(
    r"\bselect\s+(?:distinct\s+)?(?:[A-Za-z_][\w]*\s*\.\s*)?\*\s*(?:from|$)",
    re.IGNORECASE,
)

# A FROM clause referencing a table function that reads a file
# (``read_csv_auto('...')``, ``read_parquet('...')``) — a full-file scan.
_FILE_SCAN = re.compile(
    r"\bread_(?:csv|parquet|csv_auto|json|json_auto)\s*\(",
    re.IGNORECASE,
)

# Presence of a filtering / limiting clause that bounds a scan.
_HAS_BOUND = re.compile(r"\b(where|limit|using\s+sample|qualify)\b", re.IGNORECASE)


def _strip_comments(sql: str) -> str:
    """Remove ``--`` line comments and ``/* */`` block comments (best effort)."""
    no_block = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    no_line = re.sub(r"--[^\n]*", " ", no_block)
    return no_line


def lint_sql(sql: str) -> List[str]:
    """Return advisory warning strings for *sql* (empty when it looks fine)."""
    text = _strip_comments(sql or "")
    warnings: List[str] = []

    if _SELECT_STAR.search(text):
        warnings.append(
            "SELECT * projects every column — list only the columns you need "
            "for a leaner, faster scan."
        )

    if _FILE_SCAN.search(text) and not _HAS_BOUND.search(text):
        warnings.append(
            "Unfiltered full-file scan (no WHERE/LIMIT) — consider a filter or "
            "LIMIT before running this against a large file."
        )

    return warnings
