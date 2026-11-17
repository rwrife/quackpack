"""Target parsing and safe display of remote connection strings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit


class TargetError(Exception):
    """An invalid target or named connection."""


@dataclass(frozen=True)
class Target:
    kind: str
    value: str = field(repr=False)

    def redacted(self) -> str:
        if self.kind in {"file", "db"}:
            return self.value
        parts = urlsplit(self.value)
        host = parts.netloc.rsplit("@", 1)[-1]
        authority = f"<redacted>@{host}" if "@" in parts.netloc else host
        return urlunsplit(
            (parts.scheme, authority, parts.path, "<redacted>" if parts.query else "", "")
        )

    def scanner_dsn(self) -> str:
        if self.kind == "postgres":
            return self.value
        parts = urlsplit(self.value)
        assert parts.hostname is not None
        options = {"host": parts.hostname}
        if parts.port is not None:
            options["port"] = str(parts.port)
        if parts.username is not None:
            options["user"] = unquote(parts.username)
        if parts.password is not None:
            options["password"] = unquote(parts.password)
        if parts.path.lstrip("/"):
            options["database"] = unquote(parts.path[1:])
        options.update(parse_qsl(parts.query, keep_blank_values=True))
        # The scanner only recognises double-quoted key=value values, escaping
        # backslashes and double quotes. This is separate from SQL quoting.
        return " ".join(f"{key}={_connection_literal(value)}" for key, value in options.items())


def _connection_literal(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


_MYSQL_OPTIONS = {
    "socket", "ssl_mode", "ssl_ca", "ssl_capath", "ssl_cert", "ssl_cipher",
    "ssl_crl", "ssl_crlpath", "ssl_key",
}


def parse_target(value: str) -> Target:
    """Parse a remote URI or an existing-style local file/database path.

    Errors deliberately omit the supplied value, which may contain secrets.
    """
    if not value or any(ord(char) < 32 for char in value):
        raise TargetError("Target must be non-empty and contain no control characters.")
    if "://" not in value:
        kind = "db" if Path(value).suffix.lower() in {
            ".db", ".duckdb", ".sqlite", ".sqlite3"
        } else "file"
        return Target(kind, value)
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"postgres", "postgresql", "mysql"}:
            raise ValueError
        if not parts.hostname or parts.fragment or re.search(r"%(?![0-9a-fA-F]{2})", value):
            raise ValueError
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError
        if parts.scheme == "mysql":
            options = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
            if any(key not in _MYSQL_OPTIONS for key, _ in options):
                raise ValueError
        if any(ord(char) < 32 for char in unquote(value)):
            raise ValueError
    except ValueError:
        raise TargetError(
            "Invalid remote target. Use postgres:// or mysql:// with a host and valid "
            "port; MySQL URI options support socket and ssl_* settings only."
        ) from None
    kind = "mysql" if parts.scheme == "mysql" else "postgres"
    return Target(kind, urlunsplit(parts))


def resolve_options(
    target: str | None,
    db: str | Path | None,
    file: str | Path | None,
) -> tuple[str | Path | None, str | Path | None, Target | None]:
    """Resolve --target, preserving existing --db/--file combinations."""
    if target is None:
        return db, file, None
    if db is not None or file is not None:
        raise TargetError("--target cannot be combined with --db or --file.")
    if target.startswith("@"):
        from .connections import Connections

        target = Connections().get(target[1:])
    parsed = parse_target(target)
    if parsed.kind == "db":
        return parsed.value, None, None
    if parsed.kind == "file":
        return None, parsed.value, None
    return None, None, parsed
