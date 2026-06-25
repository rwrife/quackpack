"""Parameter handling for quackpack queries.

A query can embed ``:name`` style placeholders (DuckDB/SQLite prepared-statement
syntax). This module:

* **detects** those placeholder names so the catalog can record which params a
  query expects (used since M2 at ``add`` time), and
* **coerces** the string values supplied on the CLI (``--param key=value``) into
  ``int`` / ``float`` / ``str`` so numeric comparisons behave numerically (M4).

Everything here is pure and dependency-free so it is trivial to test in
isolation; the interactive prompting itself lives in the CLI layer, but the
value-coercion rules it relies on are defined here.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "extract_params",
    "to_duckdb_placeholders",
    "mask_literals",
    "coerce_value",
    "split_param_key",
    "PARAM_TYPES",
]

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


# --------------------------------------------------------------------------
# Value coercion (M4)
# --------------------------------------------------------------------------

# Explicit type annotations a user can attach to a param key, e.g.
# ``--param n:int=5`` or ``--param ratio:float=0.5``. ``str`` forces the value
# to stay textual even if it looks numeric (handy for zip codes / ids).
PARAM_TYPES: dict[str, type] = {"int": int, "float": float, "str": str}

# A param key may carry an optional ``:type`` suffix. We only treat the suffix
# as a type hint when it's one we recognise; otherwise the whole token is the
# key (so a column-ish name like ``user:id`` isn't mangled).
_KEY_TYPE_RE = re.compile(r"^(?P<key>.+?):(?P<type>[A-Za-z]+)$")

# ``float()`` accepts ``inf``/``infinity``/``nan`` (any case, optional sign).
# We exclude these from *auto* coercion so a literal param value of "nan" stays
# a string; callers wanting the IEEE value can pass an explicit ``:float`` hint.
_NON_FINITE_RE = re.compile(r"(?i)^[+-]?(inf(inity)?|nan)$")


def split_param_key(raw: str) -> tuple[str, str | None]:
    """Split a ``--param`` key into ``(name, type_hint)``.

    A trailing ``:int`` / ``:float`` / ``:str`` is interpreted as an explicit
    coercion hint; anything else leaves the key intact with no hint.

    >>> split_param_key("n")
    ('n', None)
    >>> split_param_key("n:int")
    ('n', 'int')
    >>> split_param_key("weird:name")
    ('weird:name', None)
    """
    key = raw.strip()
    m = _KEY_TYPE_RE.match(key)
    if m and m.group("type").lower() in PARAM_TYPES:
        return m.group("key").strip(), m.group("type").lower()
    return key, None


def coerce_value(value: Any, type_hint: str | None = None) -> Any:
    """Coerce a raw CLI string into ``int`` / ``float`` / ``str``.

    With no *type_hint* the value is auto-typed: it becomes an ``int`` if it
    parses cleanly as one, else a ``float`` if it parses as one, else it stays a
    ``str``. A *type_hint* of ``"str"`` forces text; ``"int"``/``"float"`` force
    the numeric type and raise :class:`ValueError` on a bad value so the CLI can
    report it. Non-string inputs (e.g. an int from an interactive default) pass
    through untouched aside from explicit hints.

    >>> coerce_value("42")
    42
    >>> coerce_value("3.14")
    3.14
    >>> coerce_value("west")
    'west'
    >>> coerce_value("007", "str")
    '007'
    >>> coerce_value("5", "float")
    5.0
    """
    if type_hint is not None:
        caster = PARAM_TYPES[type_hint]
        if caster is str:
            return value if isinstance(value, str) else str(value)
        # int("3.0") raises, which is the right call for an explicit int hint.
        return caster(str(value).strip())

    if not isinstance(value, str):
        return value

    s = value.strip()
    if s == "":
        return value
    # Try int first so whole numbers don't silently become floats.
    try:
        return int(s)
    except ValueError:
        pass
    # Auto-typing only promotes ordinary decimal floats. The special spellings
    # ``inf`` / ``nan`` that ``float()`` accepts almost always mean the *literal
    # string* when typed as a param, so we leave them as text (an explicit
    # ``:float`` hint can still force the IEEE value).
    if _NON_FINITE_RE.search(s):
        return value
    try:
        return float(s)
    except ValueError:
        return value
