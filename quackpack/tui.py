"""Headless models for the optional terminal browser (no Textual imports)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol

from .history import describe_last_run
from .params import coerce_value, extract_params
from .store import Query
from .targets import parse_target


class SearchableCatalog(Protocol):
    def search(self, text: str) -> list[Query]: ...


@dataclass(frozen=True)
class QueryListItem:
    name: str
    tags: tuple[str, ...]
    last_run: str


def build_query_list(
    catalog: SearchableCatalog,
    text: str = "",
    tag: str = "",
    *,
    now: datetime | None = None,
) -> list[QueryListItem]:
    """Combine the store's substring search with its exact-tag semantics."""
    tag = tag.strip()
    return [
        QueryListItem(
            query.name,
            tuple(query.tags),
            describe_last_run(query.last_run, query.last_status, now=now),
        )
        for query in catalog.search(text)
        if not tag or tag in query.tags
    ]


@dataclass(frozen=True)
class RunRequest:
    db: Path | None
    file: Path | None
    target: str | None = field(repr=False)
    engine: str
    params: dict[str, Any]
    missing: tuple[str, ...]


def resolve_run(
    sql: str,
    *,
    db: str = "",
    file: str = "",
    target: str = "",
    engine: str = "auto",
    values: Mapping[str, Any] | None = None,
) -> RunRequest:
    """Resolve target fields and bindings without prompting or touching disk."""
    engine = engine.strip().lower()
    if engine not in ("auto", "duckdb", "sqlite"):
        raise ValueError(f"Unknown engine {engine!r}; choose auto, duckdb, or sqlite.")
    target = target.strip()
    if target:
        if db.strip() or file.strip():
            raise ValueError("--target cannot be combined with --db or --file.")
        if not target.startswith("@"):
            parse_target(target)
    declared = extract_params(sql)
    supplied = values or {}
    params = {name: coerce_value(supplied[name]) for name in declared if name in supplied}
    return RunRequest(
        Path(db.strip()).expanduser() if db.strip() else None,
        Path(file.strip()).expanduser() if file.strip() else None,
        target or None,
        engine,
        params,
        tuple(name for name in declared if name not in params),
    )
