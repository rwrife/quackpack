"""Query execution for quackpack.

This is the *only* module that touches a database engine. Given a query, an
optional data target (a ``--db`` file or a ``--file`` CSV/Parquet/SQLite), and a
bound parameter mapping, it runs the SQL and returns columns + rows.

Two engines are supported:

* **DuckDB** (default) — reads CSV/Parquet/JSON/SQLite natively, so a ``--file``
  is exposed both as an auto-named view *and* via DuckDB's ``read_csv_auto`` /
  ``read_parquet`` table functions. This is what makes ``select * from data``
  "just work" against a CSV or Parquet path.
* **SQLite** (fallback) — used when DuckDB is unavailable or forced with
  ``--engine sqlite``. Attaches a ``--db`` SQLite file (or opens a ``.sqlite``
  ``--file``) and can ingest a CSV ``--file`` into a ``data`` table so simple
  queries still run without DuckDB present.

Parameter binding uses each driver's native ``:name`` prepared-statement
support, so values are never string-interpolated into SQL (no injection, correct
typing). Detecting params (``params.py``) and prompting/coercing them (the CLI)
happen upstream; here we simply bind the mapping we are handed.

Everything user-facing failure-wise is funneled through :class:`EngineError`
with a short, actionable message so the CLI can print it cleanly.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .params import to_duckdb_placeholders

__all__ = [
    "EngineError",
    "QueryResult",
    "ExplainResult",
    "run_query",
    "explain_query",
    "available_engines",
    "DUCKDB_AVAILABLE",
]


class EngineError(Exception):
    """A user-facing execution problem (bad path, SQL error, missing engine)."""


@dataclass
class QueryResult:
    """The outcome of running a query: column names + row tuples."""

    columns: list[str]
    rows: list[tuple]

    @property
    def rowcount(self) -> int:
        return len(self.rows)


@dataclass
class ExplainResult:
    """The outcome of an ``EXPLAIN``: the plan text plus the engine used.

    ``analyzed`` is True when the plan came from ``EXPLAIN ANALYZE`` (which
    actually executes the query and includes timing). ``engine`` records which
    backend produced the plan so callers can note SQLite's degraded form.
    """

    plan: str
    engine: str
    analyzed: bool = False


# --------------------------------------------------------------------------
# Engine availability
# --------------------------------------------------------------------------

try:  # pragma: no cover - import guard depends on environment
    import duckdb as _duckdb

    DUCKDB_AVAILABLE = True
except Exception:  # pragma: no cover
    _duckdb = None  # type: ignore[assignment]
    DUCKDB_AVAILABLE = False


def available_engines() -> list[str]:
    """Engines usable in this environment (DuckDB only if importable)."""
    engines = []
    if DUCKDB_AVAILABLE:
        engines.append("duckdb")
    engines.append("sqlite")  # always available in the stdlib
    return engines


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

# Extensions we recognise as SQLite databases when passed via --file.
_SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".duckdb"}
_PARQUET_SUFFIXES = {".parquet", ".pq"}
_CSV_SUFFIXES = {".csv", ".tsv", ".txt"}

# Conservative identifier sanitiser for the auto-generated view/table name we
# derive from a file's stem (e.g. ``my data.csv`` -> ``my_data``).
_IDENT_CLEAN = re.compile(r"\W+")


def _view_name(path: Path) -> str:
    """Derive a safe, friendly relation name from a data file's stem."""
    stem = path.stem or "data"
    cleaned = _IDENT_CLEAN.sub("_", stem).strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"data_{cleaned}" if cleaned else "data"
    return cleaned.lower()


def _classify_file(path: Path) -> str:
    """Return one of ``csv`` / ``parquet`` / ``sqlite`` for a data *path*."""
    suffix = path.suffix.lower()
    if suffix in _PARQUET_SUFFIXES:
        return "parquet"
    if suffix in _SQLITE_SUFFIXES:
        return "sqlite"
    # Default everything else (incl. .csv/.tsv and unknown) to CSV; DuckDB's
    # sniffer copes with most delimited text.
    return "csv"


def _as_path(value: Optional[str | Path]) -> Optional[Path]:
    if value is None:
        return None
    return Path(value).expanduser()


def _require_exists(path: Path, kind: str) -> None:
    if not path.exists():
        raise EngineError(f"{kind} not found: {path}")
    if path.is_dir():
        raise EngineError(f"{kind} is a directory, expected a file: {path}")


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def run_query(
    sql: str,
    *,
    db: Optional[str | Path] = None,
    file: Optional[str | Path] = None,
    params: Optional[Mapping[str, Any]] = None,
    engine: str = "auto",
) -> QueryResult:
    """Execute *sql* and return a :class:`QueryResult`.

    Parameters
    ----------
    sql:
        The query text. ``:name`` placeholders are bound from *params*.
    db:
        Path to a database file to attach (DuckDB ``.duckdb`` or a SQLite file).
    file:
        Path to a data file (CSV / Parquet / SQLite) to expose to the query.
    params:
        Mapping of placeholder name -> value, bound natively (no interpolation).
    engine:
        ``"auto"`` (DuckDB if available, else SQLite), ``"duckdb"``, or
        ``"sqlite"``.
    """
    sql = (sql or "").strip()
    if not sql:
        raise EngineError("Refusing to run an empty query.")

    chosen = _resolve_engine(engine)
    db_path = _as_path(db)
    file_path = _as_path(file)
    params = dict(params or {})

    if chosen == "duckdb":
        return _run_duckdb(sql, db_path, file_path, params)
    return _run_sqlite(sql, db_path, file_path, params)


def _resolve_engine(engine: str) -> str:
    """Validate/resolve the requested engine name to a concrete one."""
    name = (engine or "auto").strip().lower()
    if name == "auto":
        return "duckdb" if DUCKDB_AVAILABLE else "sqlite"
    if name == "duckdb":
        if not DUCKDB_AVAILABLE:
            raise EngineError(
                "DuckDB engine requested but the 'duckdb' package isn't installed. "
                "Install it or use --engine sqlite."
            )
        return "duckdb"
    if name == "sqlite":
        return "sqlite"
    raise EngineError(f"Unknown engine {engine!r}. Choose 'auto', 'duckdb', or 'sqlite'.")


def explain_query(
    sql: str,
    *,
    db: Optional[str | Path] = None,
    file: Optional[str | Path] = None,
    params: Optional[Mapping[str, Any]] = None,
    engine: str = "auto",
    analyze: bool = False,
) -> ExplainResult:
    """Return the query plan for *sql* without returning its result rows.

    Mirrors :func:`run_query`'s target/param handling but wraps the SQL in
    ``EXPLAIN`` (or ``EXPLAIN ANALYZE`` when *analyze* is set). DuckDB produces
    a rich physical plan; the SQLite fallback degrades to ``EXPLAIN QUERY
    PLAN`` (and ignores *analyze*, which it doesn't support the same way).
    """
    sql = (sql or "").strip()
    if not sql:
        raise EngineError("Refusing to explain an empty query.")

    chosen = _resolve_engine(engine)
    db_path = _as_path(db)
    file_path = _as_path(file)
    params = dict(params or {})

    if chosen == "duckdb":
        return _explain_duckdb(sql, db_path, file_path, params, analyze)
    return _explain_sqlite(sql, db_path, file_path, params)


# --------------------------------------------------------------------------
# DuckDB backend
# --------------------------------------------------------------------------


def _run_duckdb(
    sql: str,
    db_path: Optional[Path],
    file_path: Optional[Path],
    params: Mapping[str, Any],
) -> QueryResult:
    assert _duckdb is not None  # narrowed by caller via DUCKDB_AVAILABLE

    # Connect to the requested db file or an in-memory database.
    if db_path is not None:
        _require_exists(db_path, "Database")
        try:
            con = _duckdb.connect(str(db_path), read_only=True)
        except Exception as exc:  # pragma: no cover - driver/edge errors
            raise EngineError(f"Could not open DuckDB database {db_path}: {exc}")
    else:
        con = _duckdb.connect(database=":memory:")

    try:
        if file_path is not None:
            _attach_file_duckdb(con, file_path)
        return _execute_duckdb(con, sql, params)
    finally:
        con.close()


def _sql_str_literal(value: str) -> str:
    """Quote *value* as a SQL string literal (doubling embedded quotes).

    Used only for file *paths* in DDL/table-function positions where engines
    don't accept bound parameters (e.g. ``CREATE VIEW ... read_csv_auto(...)``).
    Paths come from the user's own ``--file`` arg, but we quote defensively so a
    path containing a quote can't break out of the literal.
    """
    return "'" + value.replace("'", "''") + "'"


def _attach_file_duckdb(con: Any, file_path: Path) -> None:
    """Expose *file_path* as a queryable relation in the DuckDB connection."""
    _require_exists(file_path, "File")
    kind = _classify_file(file_path)
    name = _view_name(file_path)
    literal = _sql_str_literal(str(file_path))

    try:
        if kind == "parquet":
            con.execute(
                f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet({literal})"
            )
        elif kind == "sqlite":
            # Attach the SQLite db so its tables are visible by name.
            con.execute("INSTALL sqlite")  # no-op if bundled
            con.execute("LOAD sqlite")
            con.execute(f"ATTACH {literal} AS {name} (TYPE sqlite)")
        else:  # csv / delimited text
            con.execute(
                f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_csv_auto({literal})"
            )
    except Exception as exc:
        raise EngineError(f"Could not read {file_path}: {exc}")


def _execute_duckdb(con: Any, sql: str, params: Mapping[str, Any]) -> QueryResult:
    # DuckDB names parameters with ``$name``; quackpack stores ``:name``. Only
    # rewrite when the caller actually supplied params, so plain SQL (which may
    # legitimately contain a stray colon DuckDB tolerates) is passed untouched.
    if params:
        prepared_sql = to_duckdb_placeholders(sql)
        try:
            cur = con.execute(prepared_sql, dict(params))
        except Exception as exc:
            raise EngineError(_format_sql_error(exc))
    else:
        try:
            cur = con.execute(sql)
        except Exception as exc:
            raise EngineError(_format_sql_error(exc))
    description = cur.description or []
    columns = [d[0] for d in description]
    rows = [tuple(r) for r in cur.fetchall()]
    return QueryResult(columns=columns, rows=rows)


def _explain_duckdb(
    sql: str,
    db_path: Optional[Path],
    file_path: Optional[Path],
    params: Mapping[str, Any],
    analyze: bool,
) -> ExplainResult:
    assert _duckdb is not None

    if db_path is not None:
        _require_exists(db_path, "Database")
        try:
            con = _duckdb.connect(str(db_path), read_only=True)
        except Exception as exc:  # pragma: no cover - driver/edge errors
            raise EngineError(f"Could not open DuckDB database {db_path}: {exc}")
    else:
        con = _duckdb.connect(database=":memory:")

    try:
        if file_path is not None:
            _attach_file_duckdb(con, file_path)
        keyword = "EXPLAIN ANALYZE" if analyze else "EXPLAIN"
        explain_sql = f"{keyword} {sql}"
        if params:
            explain_sql = to_duckdb_placeholders(explain_sql)
            try:
                cur = con.execute(explain_sql, dict(params))
            except Exception as exc:
                raise EngineError(_format_sql_error(exc))
        else:
            try:
                cur = con.execute(explain_sql)
            except Exception as exc:
                raise EngineError(_format_sql_error(exc))
        rows = cur.fetchall()
    finally:
        con.close()

    plan = _render_explain_rows(rows)
    return ExplainResult(plan=plan, engine="duckdb", analyzed=analyze)


def _render_explain_rows(rows: Iterable[tuple]) -> str:
    """Flatten EXPLAIN output rows into a single plan string.

    DuckDB returns rows like ``(explain_key, explain_value)``; the plan text is
    the last column. SQLite's ``EXPLAIN QUERY PLAN`` returns wider rows we
    join instead. We select the last cell per row, which is the plan body in
    both engines' EXPLAIN forms.
    """
    parts: list[str] = []
    for row in rows:
        if not row:
            continue
        parts.append(str(row[-1]))
    return "\n".join(parts).strip()


# --------------------------------------------------------------------------
# SQLite backend
# --------------------------------------------------------------------------


def _run_sqlite(
    sql: str,
    db_path: Optional[Path],
    file_path: Optional[Path],
    params: Mapping[str, Any],
) -> QueryResult:
    # Decide what to connect to. Priority: an explicit --db, else a SQLite
    # --file, else an in-memory db (into which a CSV --file can be loaded).
    sqlite_target: Optional[Path] = None
    csv_to_load: Optional[Path] = None

    if db_path is not None:
        _require_exists(db_path, "Database")
        sqlite_target = db_path
    if file_path is not None:
        _require_exists(file_path, "File")
        kind = _classify_file(file_path)
        if kind == "sqlite":
            if sqlite_target is None:
                sqlite_target = file_path
            else:
                # Both a db and a sqlite file: attach the file alongside.
                pass
        elif kind == "csv":
            csv_to_load = file_path
        else:  # parquet
            raise EngineError(
                "The SQLite engine can't read Parquet files. "
                "Use DuckDB (the default) for Parquet, or pass a CSV/SQLite file."
            )

    try:
        con = sqlite3.connect(str(sqlite_target) if sqlite_target else ":memory:")
    except sqlite3.Error as exc:  # pragma: no cover
        raise EngineError(f"Could not open SQLite database: {exc}")

    try:
        # Attach a SQLite --file when a separate --db was already opened.
        if (
            file_path is not None
            and _classify_file(file_path) == "sqlite"
            and sqlite_target is not None
            and file_path != sqlite_target
        ):
            con.execute("ATTACH DATABASE ? AS extra", [str(file_path)])
        if csv_to_load is not None:
            _load_csv_into_sqlite(con, csv_to_load, _view_name(csv_to_load))
        return _execute_sqlite(con, sql, params)
    finally:
        con.close()


def _infer_sqlite_type(values: Iterable[Any]) -> str:
    """Pick an SQLite column affinity (INTEGER/REAL/TEXT) from sample *values*.

    Empty strings are treated as missing (ignored). If every non-empty sample
    parses as an int we use INTEGER; if they all parse as float we use REAL;
    otherwise TEXT. This makes ``amount > :n`` behave numerically in the SQLite
    fallback instead of comparing strings lexicographically.
    """
    saw_value = False
    all_int = True
    all_float = True
    for v in values:
        if v is None or v == "":
            continue
        saw_value = True
        s = str(v).strip()
        if all_int:
            try:
                int(s)
            except ValueError:
                all_int = False
        if all_float:
            try:
                float(s)
            except ValueError:
                all_float = False
        if not all_int and not all_float:
            return "TEXT"
    if not saw_value:
        return "TEXT"
    if all_int:
        return "INTEGER"
    if all_float:
        return "REAL"
    return "TEXT"


def _coerce(value: Any, affinity: str) -> Any:
    """Convert a raw CSV string into the chosen affinity (best effort)."""
    if value is None or value == "":
        return None
    if affinity == "INTEGER":
        try:
            return int(str(value).strip())
        except ValueError:
            return value
    if affinity == "REAL":
        try:
            return float(str(value).strip())
        except ValueError:
            return value
    return value


def _load_csv_into_sqlite(con: sqlite3.Connection, path: Path, table: str) -> None:
    """Ingest a CSV into a SQLite table so the fallback engine can query it.

    Column affinities are inferred from a sample of the data (INTEGER/REAL/TEXT)
    so numeric filters behave as expected. DuckDB remains the right tool for
    typed CSV/Parquet work; this is a graceful degradation when it's absent.
    """
    import csv as _csv

    try:
        with path.open("r", newline="", encoding="utf-8") as fh:
            reader = _csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                header = []
            if not header:
                raise EngineError(f"CSV {path} has no header row to load.")
            cols = [_IDENT_CLEAN.sub("_", h).strip("_") or f"c{i}" for i, h in enumerate(header)]
            data_rows = list(reader)

        # Normalise ragged rows to the header width.
        norm_rows: list[list[Any]] = []
        for row in data_rows:
            if len(row) < len(cols):
                row = list(row) + [None] * (len(cols) - len(row))
            elif len(row) > len(cols):
                row = list(row[: len(cols)])
            norm_rows.append(list(row))

        # Infer affinity per column from up to 1000 sampled rows.
        sample = norm_rows[:1000]
        affinities = [
            _infer_sqlite_type(r[i] for r in sample) for i in range(len(cols))
        ]

        col_defs = ", ".join(f'"{c}" {affinities[i]}' for i, c in enumerate(cols))
        con.execute(f'CREATE TABLE "{table}" ({col_defs})')
        placeholders = ", ".join("?" for _ in cols)
        insert = f'INSERT INTO "{table}" VALUES ({placeholders})'
        coerced = [
            tuple(_coerce(r[i], affinities[i]) for i in range(len(cols)))
            for r in norm_rows
        ]
        con.executemany(insert, coerced)
        con.commit()
    except EngineError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise EngineError(f"Could not read {path}: {exc}")


def _execute_sqlite(con: sqlite3.Connection, sql: str, params: Mapping[str, Any]) -> QueryResult:
    try:
        cur = con.execute(sql, dict(params)) if params else con.execute(sql)
    except sqlite3.Error as exc:
        raise EngineError(_format_sql_error(exc))
    description = cur.description or []
    columns = [d[0] for d in description]
    rows = [tuple(r) for r in cur.fetchall()]
    return QueryResult(columns=columns, rows=rows)


def _explain_sqlite(
    sql: str,
    db_path: Optional[Path],
    file_path: Optional[Path],
    params: Mapping[str, Any],
) -> ExplainResult:
    """Degrade to ``EXPLAIN QUERY PLAN`` for the SQLite fallback.

    SQLite has no ``EXPLAIN ANALYZE``; ``EXPLAIN QUERY PLAN`` gives a compact,
    human-useful plan. We reuse the same target-attach logic as a normal run by
    delegating the connection setup through ``_run_sqlite``-style handling.
    """
    sqlite_target: Optional[Path] = None
    csv_to_load: Optional[Path] = None

    if db_path is not None:
        _require_exists(db_path, "Database")
        sqlite_target = db_path
    if file_path is not None:
        _require_exists(file_path, "File")
        kind = _classify_file(file_path)
        if kind == "sqlite":
            if sqlite_target is None:
                sqlite_target = file_path
        elif kind == "csv":
            csv_to_load = file_path
        else:  # parquet
            raise EngineError(
                "The SQLite engine can't read Parquet files. "
                "Use DuckDB (the default) for Parquet, or pass a CSV/SQLite file."
            )

    try:
        con = sqlite3.connect(str(sqlite_target) if sqlite_target else ":memory:")
    except sqlite3.Error as exc:  # pragma: no cover
        raise EngineError(f"Could not open SQLite database: {exc}")

    try:
        if (
            file_path is not None
            and _classify_file(file_path) == "sqlite"
            and sqlite_target is not None
            and file_path != sqlite_target
        ):
            con.execute("ATTACH DATABASE ? AS extra", [str(file_path)])
        if csv_to_load is not None:
            _load_csv_into_sqlite(con, csv_to_load, _view_name(csv_to_load))
        explain_sql = f"EXPLAIN QUERY PLAN {sql}"
        try:
            cur = con.execute(explain_sql, dict(params)) if params else con.execute(explain_sql)
        except sqlite3.Error as exc:
            raise EngineError(_format_sql_error(exc))
        rows = cur.fetchall()
    finally:
        con.close()

    plan = _render_explain_rows(rows)
    return ExplainResult(plan=plan, engine="sqlite", analyzed=False)


# --------------------------------------------------------------------------
# Error formatting
# --------------------------------------------------------------------------


def _format_sql_error(exc: Exception) -> str:
    """Turn a driver exception into a concise, single-line SQL error message."""
    msg = str(exc).strip().splitlines()
    text = msg[0] if msg else exc.__class__.__name__
    return f"SQL error: {text}"
