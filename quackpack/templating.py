"""Query templating / composition for quackpack (backlog #10).

Lets one saved query reference another so you can factor common cleaning/joins
into reusable building blocks instead of copy-pasting SQL. A reference looks
like ``{{ other_query }}`` and expands, in place, to the *fully resolved* SQL of
that query wrapped in parentheses — i.e. it inlines as a subquery/CTE-body, so
it drops cleanly into a ``from``/``join``/``with`` position::

    -- saved as "orders_clean":
    select * from read_csv_auto(:file) where status <> 'void'

    -- saved as "big_orders":
    select * from {{ orders_clean }} where amount > :threshold

    quackpack show --expanded big_orders
    -> select * from (select * from read_csv_auto(:file) where status <> 'void')
       where amount > :threshold

Design notes
------------
* **Pure by default.** The core :func:`expand` takes a ``resolve(name) -> sql``
  callable rather than a :class:`~quackpack.store.Catalog`, so the expansion
  logic is dependency-free and trivial to unit-test. :func:`expand_query` is a
  thin convenience wrapper for the CLI that binds a catalog.
* **Literal-safe.** ``{{ ... }}`` inside a string/identifier literal is left
  untouched, reusing the same masking rules as the ``:param`` scanner so a
  literal like ``'{{ not_a_ref }}'`` never triggers a lookup.
* **Cycle detection.** A query that (transitively) references itself raises
  :class:`TemplateCycleError` with the offending path instead of recursing
  forever.
* **Resolve once.** Expansion runs to a single flat SQL string before the
  engine ever sees it; params (``:name``) are preserved verbatim and reconciled
  by the existing params layer after expansion.
"""

from __future__ import annotations

import re
from typing import Callable, List

from .params import mask_literals

__all__ = [
    "TEMPLATE_RE",
    "extract_refs",
    "has_refs",
    "expand",
    "expand_query",
    "TemplateError",
    "TemplateNotFoundError",
    "TemplateCycleError",
]

# A reference is ``{{ name }}`` where *name* is a query name: a leading
# letter/underscore then word chars or hyphens (query names in the wild use
# hyphens, e.g. ``top-regions``). Surrounding whitespace inside the braces is
# ignored. We intentionally keep the grammar tight so stray ``{{`` / ``}}`` in
# ordinary SQL (rare, but possible in string building) doesn't misfire — an
# unmatched or malformed brace pair is simply left as literal text.
TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}")

# Cap recursion so a pathological (but acyclic) chain can't blow the stack; far
# deeper than any sane personal query library would ever nest.
_MAX_DEPTH = 64


class TemplateError(Exception):
    """Base class for query-templating problems."""


class TemplateNotFoundError(TemplateError):
    """Raised when a ``{{ ref }}`` names a query that doesn't exist."""


class TemplateCycleError(TemplateError):
    """Raised when query references form a cycle (direct or transitive)."""


def _iter_refs(sql: str):
    """Yield ``(match, name)`` for each real ``{{ ref }}`` in *sql*.

    References inside string/identifier literals are skipped by scanning a
    literal-masked copy of the SQL (spaces of equal length), then splicing
    against the original so replacements preserve literal contents verbatim.
    """
    masked = mask_literals(sql)
    for match in TEMPLATE_RE.finditer(masked):
        yield match, match.group(1)


def extract_refs(sql: str) -> List[str]:
    """Return the distinct ``{{ ref }}`` names in *sql*, in first-seen order.

    Literal-safe: a reference inside a quoted string/identifier is ignored.

    >>> extract_refs("select * from {{ base }} join {{ dims }} using (id)")
    ['base', 'dims']
    >>> extract_refs("select '{{ nope }}' as t, * from {{ base }}")
    ['base']
    >>> extract_refs("select 1")
    []
    """
    seen: dict[str, None] = {}
    for _match, name in _iter_refs(sql):
        seen.setdefault(name, None)
    return list(seen)


def has_refs(sql: str) -> bool:
    """Return ``True`` if *sql* contains at least one real ``{{ ref }}``.

    >>> has_refs("select * from {{ base }}")
    True
    >>> has_refs("select '{{ x }}'")
    False
    """
    for _match, _name in _iter_refs(sql):
        return True
    return False


def expand(
    sql: str,
    resolve: Callable[[str], str],
    *,
    _root: str | None = None,
    _stack: tuple[str, ...] = (),
    _depth: int = 0,
) -> str:
    """Recursively expand ``{{ ref }}`` templates in *sql* to a flat SQL string.

    *resolve* maps a referenced query name to its (unexpanded) SQL. Each
    reference is replaced by the *fully expanded* SQL of the target wrapped in
    parentheses, so it slots in as a subquery. ``:param`` placeholders are left
    untouched for the params layer to bind after expansion.

    Raises :class:`TemplateNotFoundError` when *resolve* can't find a name and
    :class:`TemplateCycleError` when a reference chain loops back on itself. The
    ``_root`` / ``_stack`` / ``_depth`` arguments are internal recursion state
    and should not be supplied by callers.

    >>> pack = {
    ...     "base": "select * from t where ok",
    ...     "wrap": "select * from {{ base }} limit :n",
    ... }
    >>> expand(pack["wrap"], pack.__getitem__)
    'select * from (select * from t where ok) limit :n'
    """
    if _depth > _MAX_DEPTH:
        # Depth guard is a backstop; genuine cycles are caught below with a
        # precise path. This only trips on absurdly deep *acyclic* nesting.
        chain = " -> ".join((*_stack, "…")) or "(query)"
        raise TemplateCycleError(
            f"Template nesting too deep (> {_MAX_DEPTH}) starting at {chain}."
        )

    out: List[str] = []
    last = 0
    for match, name in _iter_refs(sql):
        start, end = match.span()
        out.append(sql[last:start])

        # Cycle check: is this name already on the resolution path?
        if name in _stack:
            cycle = " -> ".join((*_stack, name))
            raise TemplateCycleError(f"Query reference cycle: {cycle}.")

        try:
            inner_sql = resolve(name)
        except KeyError:
            raise TemplateNotFoundError(
                f"Referenced query {name!r} does not exist."
            ) from None

        expanded_inner = expand(
            inner_sql,
            resolve,
            _root=_root or name,
            _stack=(*_stack, name),
            _depth=_depth + 1,
        )
        # Wrap in parens so the inlined query is a valid subquery/derived table.
        out.append(f"({expanded_inner.strip()})")
        last = end

    out.append(sql[last:])
    return "".join(out)


def expand_query(catalog, name: str) -> str:
    """Expand the stored query *name* from *catalog* to flat, runnable SQL.

    Convenience wrapper over :func:`expand` that resolves references through the
    catalog. Looks up query bodies by name and raises
    :class:`TemplateNotFoundError` / :class:`TemplateCycleError` on bad refs or
    cycles. The starting query itself is tracked in the cycle path so a query
    that references itself is reported cleanly.

    *catalog* only needs a ``get(name) -> Query`` method (duck-typed), so this
    stays decoupled from the concrete :class:`~quackpack.store.Catalog`.
    """
    from .store import QueryNotFoundError  # local import avoids a cycle

    def resolve(ref: str) -> str:
        try:
            return catalog.get(ref).sql
        except QueryNotFoundError:
            raise KeyError(ref) from None

    root = catalog.get(name)  # let a missing root raise the catalog's own error
    return expand(root.sql, resolve, _root=name, _stack=(name,))
