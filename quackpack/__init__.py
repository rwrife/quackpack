"""quackpack — a personal pantry for your SQL.

Stash any ad-hoc DuckDB/SQLite query, give it a name, tags, and ``:params``,
then rerun it instantly against any data file.
"""

from __future__ import annotations

__all__ = ["__version__"]


def _resolve_version() -> str:
    """Resolve the installed package version, with a sane dev fallback.

    Prefers installed distribution metadata so the CLI ``--version`` always
    matches what was packaged. Falls back to the in-tree value when running
    from a source checkout that hasn't been installed.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("quackpack")
    except Exception:  # pragma: no cover - exercised only when uninstalled
        return "0.1.0"


__version__ = _resolve_version()
