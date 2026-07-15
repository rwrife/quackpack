"""Glob / multi-file fan-out for quackpack (issue #32).

A single stored query can be run across *many* data files by passing a glob to
``--file`` (e.g. ``--file 'logs/*.parquet'``). quackpack expands the glob and
executes the query once per matching file, then either **UNION**-s the results
into one table (the default) or returns a per-file list of labelled results
(``--per-file``).

Why per-file execution instead of handing the glob straight to DuckDB's
``read_parquet('glob')``? Stored queries reference the file by its auto-derived
relation name (the file's *stem*), and a glob has no single sensible stem. Running
per-file keeps the existing "``select * from data``"/stem-name ergonomics working
unchanged, makes the behaviour identical across DuckDB and the SQLite fallback,
and gives us a clean hook for the ``_source_file`` provenance column. The cost —
N executions instead of one — is fine for the "muscle memory" scale this targets.

Everything funnels failures through :class:`~quackpack.engine.EngineError` so the
CLI prints them cleanly.
"""

from __future__ import annotations

import glob as _glob
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from .engine import EngineError, QueryResult, run_query

__all__ = [
    "is_glob",
    "expand_glob",
    "GlobRun",
    "run_query_multi",
    "SOURCE_COLUMN",
]

# The magic characters that make a --file value a glob rather than a plain path.
_GLOB_CHARS = set("*?[")

# Implicit provenance column injected with --with-source.
SOURCE_COLUMN = "_source_file"


def is_glob(value: str | Path | None) -> bool:
    """True when *value* looks like a shell glob pattern (``*``/``?``/``[``)."""
    if value is None:
        return False
    return any(ch in _GLOB_CHARS for ch in str(value))


def expand_glob(pattern: str | Path) -> list[Path]:
    """Expand *pattern* into a sorted list of existing file paths.

    Directories are skipped (a query runs against files). Ordering is
    deterministic (lexicographic) so UNION output and per-file labelling are
    stable across runs.
    """
    text = os.path.expanduser(str(pattern))
    matches = _glob.glob(text, recursive=True)
    files = sorted(Path(m) for m in matches if Path(m).is_file())
    return files


@dataclass
class GlobRun:
    """Result of a multi-file run.

    ``per_file`` holds one ``(path, QueryResult)`` pair per matched file, in the
    order the files were executed. ``combined`` is the UNION-ed result (all rows
    concatenated under a shared column set); it is ``None`` when the caller asked
    for per-file output only.
    """

    per_file: list[tuple[Path, QueryResult]]
    combined: Optional[QueryResult]

    @property
    def file_count(self) -> int:
        return len(self.per_file)


def _with_source(result: QueryResult, source: str) -> QueryResult:
    """Return a copy of *result* with a leading ``_source_file`` column added."""
    columns = [SOURCE_COLUMN, *result.columns]
    rows = [(source, *row) for row in result.rows]
    return QueryResult(columns=columns, rows=rows)


def _assert_union_compatible(
    reference: QueryResult, other: QueryResult, ref_path: Path, other_path: Path
) -> None:
    """Guard that two per-file results share a column shape before UNION.

    A UNION across heterogeneous schemas is almost always a mistake, so we fail
    loudly with the offending files rather than silently mismatching cells.
    """
    if reference.columns != other.columns:
        raise EngineError(
            "Cannot UNION results with different columns across files.\n"
            f"  {ref_path}: {reference.columns}\n"
            f"  {other_path}: {other.columns}\n"
            "Use --per-file to render each file separately."
        )


def run_query_multi(
    sql: str,
    *,
    pattern: str | Path,
    db: Optional[str | Path] = None,
    params: Optional[Mapping[str, Any]] = None,
    engine: str = "auto",
    per_file: bool = False,
    with_source: bool = False,
    view_as: str = "data",
) -> GlobRun:
    """Run *sql* across every file matching *pattern*.

    Parameters
    ----------
    sql:
        The query text (``:name`` placeholders bound from *params*).
    pattern:
        A glob for ``--file``. Expanded to concrete files; a zero-match glob is
        an :class:`EngineError`.
    db:
        Optional database file, attached for every per-file run (rare with globs
        but supported for symmetry with :func:`run_query`).
    params, engine:
        Forwarded to :func:`run_query` unchanged.
    per_file:
        When True, skip building the combined UNION and return only the labelled
        per-file results.
    with_source:
        When True, prepend a ``_source_file`` column (the file path) to every
        row for provenance. Applies to both per-file and combined output.
    view_as:
        Stable relation alias each file is exposed under (default ``data``), so
        one stored query runs unchanged across files with differing stems —\n        write it as ``select * from data`` rather than a per-file stem name.
    """
    files = expand_glob(pattern)
    if not files:
        raise EngineError(
            f"Glob matched no files: {pattern}\n"
            "Check the pattern and quoting (wrap it in quotes so your shell "
            "doesn't expand it first, e.g. --file 'logs/*.parquet')."
        )

    per: list[tuple[Path, QueryResult]] = []
    for path in files:
        result = run_query(
            sql, db=db, file=path, params=params, engine=engine, view_as=view_as
        )
        if with_source:
            result = _with_source(result, str(path))
        per.append((path, result))

    combined: Optional[QueryResult] = None
    if not per_file:
        ref_path, ref_result = per[0]
        all_rows: list[tuple] = list(ref_result.rows)
        for path, result in per[1:]:
            _assert_union_compatible(ref_result, result, ref_path, path)
            all_rows.extend(result.rows)
        combined = QueryResult(columns=list(ref_result.columns), rows=all_rows)

    return GlobRun(per_file=per, combined=combined)
