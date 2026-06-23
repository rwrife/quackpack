"""Parameter placeholder detection for quackpack queries.

A query can embed ``:name`` style placeholders (DuckDB/SQLite prepared-statement
syntax). This module extracts those placeholder names so the catalog can record
which params a query expects. Binding/prompting for values lands in M4 — for M2
we only need to *detect* them at ``add`` time.

Kept dependency-free and pure so it is trivial to test in isolation.
"""

from __future__ import annotations

import re

__all__ = ["extract_params", "to_duckdb_placeholders", "mask_literals"]

# A :param is a colon followed by an identifier (letter/underscore, then word
# chars). We deliberately ignore:
#   - ``::`` casts (DuckDB/Postgres ``value::INT``) — handled by the negative
#     lookbehind for a preceding colon.
#   - leading-double-colon placeholders themselves.
# Anything inside string/identifier quotes is stripped before matching so a
# literal like ``'12:30'`` or ``"a:b"`` never registers as a param.
_PARAM_RE = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")

# Matches single-quoted strings, double-quoted identifiers, and dollar-quoted
# string literals so their contents are excluded from param scanning.
_MASK_RE = re.compile(
    r"""
      '(?:[^']|'')*'          # single-quoted string (with '' escapes)
    | "(?:[^"]|"")*"          # double-quoted identifier
    | \$\$.*?\$\$             # $$ dollar-quoted $$ (non-greedy, DOTALL)
    """,
    re.VERBOSE | re.DOTALL,
)


def _mask_literals(sql: str) -> str:
    """Replace quoted strings/identifiers with spaces of equal length.

    Equal-length replacement keeps any positional reasoning intact while making
    sure colons inside literals can't be misread as parameters.
    """
    return _MASK_RE.sub(lambda m: " " * len(m.group(0)), sql)


# Public alias so other modules (e.g. the engine) can reuse literal masking
# without reaching into a private name.
mask_literals = _mask_literals


def extract_params(sql: str) -> list[str]:
    """Return the distinct ``:param`` names in *sql*, in first-seen order.

    >>> extract_params("select * from t where id = :id and ts > :since")
    ['id', 'since']
    >>> extract_params("select 1::int")
    []
    >>> extract_params("select '12:30' as t, :real_one")
    ['real_one']
    """
    masked = _mask_literals(sql)
    seen: dict[str, None] = {}
    for match in _PARAM_RE.finditer(masked):
        seen.setdefault(match.group(1), None)
    return list(seen)


def to_duckdb_placeholders(sql: str) -> str:
    """Rewrite ``:name`` placeholders to DuckDB's ``$name`` syntax.

    quackpack's catalog standardises on ``:param`` (SQLite/Postgres style), but
    DuckDB names parameters with ``$name``. This converts only *real* params —
    colons inside string/identifier literals and ``::casts`` are left untouched,
    using the same masking rules as :func:`extract_params`.

    >>> to_duckdb_placeholders("select * from t where id = :id")
    'select * from t where id = $id'
    >>> to_duckdb_placeholders("select 1::int as x, :n")
    'select 1::int as x, $n'
    >>> to_duckdb_placeholders("select '12:30' as t, :real_one")
    "select '12:30' as t, $real_one"
    """
    masked = _mask_literals(sql)

    # Walk matches on the masked text but splice replacements into the original
    # so literal contents are preserved verbatim.
    out: list[str] = []
    last = 0
    for match in _PARAM_RE.finditer(masked):
        start, end = match.span()
        out.append(sql[last:start])
        out.append("$" + match.group(1))
        last = end
    out.append(sql[last:])
    return "".join(out)
