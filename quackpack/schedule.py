"""Emit (and optionally install) a cron line that reruns a saved query.

Backlog #11 / issue #33. quackpack stays a *small sharp tool*: instead of
shipping a daemon or scheduler, ``quackpack schedule`` just builds a ready-to
-paste crontab line that reruns a saved query (via ``quackpack run``) and
redirects the result to a file on whatever schedule you give it. Optionally
``--install`` appends that line to the user's crontab, idempotently and behind
a confirmation guard, marking each managed line with a sentinel comment so
``--list`` / ``--remove`` only ever touch quackpack's own entries.

This module is deliberately I/O-light: the interesting parts are

* :func:`build_run_command` / :func:`build_cron_line` — pure string building,
  fully unit-testable with no subprocess or crontab access; and
* :func:`read_crontab` / :func:`write_crontab` — the only functions that shell
  out to ``crontab``, kept tiny so tests can monkeypatch them.

The higher-level :func:`install_line`, :func:`list_lines` and
:func:`remove_line` compose those two layers and are exercised in tests with a
mocked crontab.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence

__all__ = [
    "SENTINEL",
    "CronError",
    "ManagedLine",
    "validate_cron_expr",
    "build_run_command",
    "build_cron_line",
    "read_crontab",
    "write_crontab",
    "install_line",
    "list_lines",
    "remove_line",
]

# Every quackpack-managed crontab line carries a trailing sentinel comment so
# --list / --remove can operate on *only* our lines and never touch a user's
# hand-written entries. The query name is embedded for human-readable listing.
SENTINEL = "# quackpack:schedule"

_FORMATS = ("csv", "json", "table")


class CronError(Exception):
    """Raised for invalid schedules or crontab manipulation failures."""


# --------------------------------------------------------------------------
# Cron expression validation (lightweight, standard 5-field)
# --------------------------------------------------------------------------

# Ranges for the five standard crontab fields.
_FIELD_BOUNDS = (
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 7),   # day of week (0 and 7 both == Sunday)
)


def _validate_field(field: str, lo: int, hi: int) -> bool:
    """Return True if one crontab *field* is well-formed within [lo, hi].

    Supports ``*``, ``*/step``, ``a``, ``a-b``, ``a-b/step`` and comma lists of
    those. This is intentionally permissive but catches obvious garbage (empty
    fields, out-of-range numbers, backwards ranges).
    """
    if field == "":
        return False
    for part in field.split(","):
        if part == "":
            return False
        step: Optional[str] = None
        if "/" in part:
            part, step = part.split("/", 1)
            if not step.isdigit() or int(step) == 0:
                return False
        if part == "*":
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                return False
            ai, bi = int(a), int(b)
            if ai > bi or ai < lo or bi > hi:
                return False
        else:
            if not part.isdigit():
                return False
            v = int(part)
            if v < lo or v > hi:
                return False
    return True


def validate_cron_expr(expr: str) -> str:
    """Validate a standard 5-field cron *expr*, returning it normalised.

    Collapses internal whitespace to single spaces. Raises :class:`CronError`
    with a clear message on anything that isn't five well-formed fields.
    """
    fields = expr.split()
    if len(fields) != 5:
        raise CronError(
            f"Cron expression must have 5 fields "
            f"(min hour dom month dow); got {len(fields)}: {expr!r}."
        )
    for field, (lo, hi) in zip(fields, _FIELD_BOUNDS):
        if not _validate_field(field, lo, hi):
            raise CronError(f"Invalid cron field {field!r} in {expr!r}.")
    return " ".join(fields)


# --------------------------------------------------------------------------
# Command / line building (pure)
# --------------------------------------------------------------------------


def build_run_command(
    name: str,
    *,
    file: Optional[str] = None,
    db: Optional[str] = None,
    fmt: str = "csv",
    params: Optional[Sequence[str]] = None,
    preset: Optional[str] = None,
    out: Optional[str] = None,
    quackpack_bin: str = "quackpack",
) -> str:
    """Build the ``quackpack run ...`` shell command (string) for a schedule.

    Threads ``--file``/``--db``, ``--format``, ``--param`` (repeatable) and
    ``--preset`` through exactly as a user would type them, always adds
    ``--no-input`` (cron has no TTY) and ``--no-snapshot`` (a scheduled extract
    shouldn't churn the diff cache), and redirects to *out* when given. Every
    interpolated value is ``shlex.quote``-escaped so paths with spaces / odd
    characters survive the crontab round-trip.
    """
    if fmt.lower() not in _FORMATS:
        raise CronError(
            f"Unknown --format {fmt!r}. Choose one of: {', '.join(_FORMATS)}."
        )
    parts: List[str] = [quackpack_bin, "run", shlex.quote(name)]
    if db is not None:
        parts += ["--db", shlex.quote(db)]
    if file is not None:
        parts += ["--file", shlex.quote(file)]
    if preset is not None:
        parts += ["--preset", shlex.quote(preset)]
    for p in params or []:
        parts += ["--param", shlex.quote(p)]
    parts += ["--format", shlex.quote(fmt.lower())]
    # cron has no TTY: never prompt, and don't disturb the snapshot cache.
    parts += ["--no-input", "--no-snapshot"]
    cmd = " ".join(parts)
    if out is not None:
        cmd += f" > {shlex.quote(out)}"
    return cmd


def build_cron_line(
    expr: str,
    command: str,
    *,
    name: str,
) -> str:
    """Combine a validated cron *expr* and *command* into one crontab line.

    Appends the sentinel comment (with the query *name*) so quackpack can later
    recognise and manage the line. The expression is validated here so callers
    get a single choke point.
    """
    norm = validate_cron_expr(expr)
    tag = f"{SENTINEL} {name}".rstrip()
    return f"{norm} {command} {tag}"


# --------------------------------------------------------------------------
# Managed-line parsing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ManagedLine:
    """A parsed quackpack-managed crontab entry."""

    raw: str
    name: str
    index: int  # position among *managed* lines (0-based)


_SENTINEL_RE = re.compile(re.escape(SENTINEL) + r"(?:\s+(?P<name>\S+))?\s*$")


def _managed_name(line: str) -> Optional[str]:
    """Return the query name for a managed *line*, or None if not managed."""
    m = _SENTINEL_RE.search(line)
    if not m:
        return None
    return m.group("name") or ""


def parse_managed(lines: Iterable[str]) -> List[ManagedLine]:
    """Extract quackpack-managed entries (in order) from crontab *lines*."""
    out: List[ManagedLine] = []
    idx = 0
    for raw in lines:
        name = _managed_name(raw)
        if name is not None:
            out.append(ManagedLine(raw=raw.rstrip("\n"), name=name, index=idx))
            idx += 1
    return out


# --------------------------------------------------------------------------
# crontab I/O (the only functions that shell out)
# --------------------------------------------------------------------------

# Injected so tests can swap in a fake crontab. Signature mirrors the thin
# wrappers below.
CrontabReader = Callable[[], str]
CrontabWriter = Callable[[str], None]


def read_crontab() -> str:
    """Return the current user's crontab text ("" if none is installed)."""
    import subprocess

    proc = subprocess.run(
        ["crontab", "-l"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # `crontab -l` exits non-zero with "no crontab for <user>" when empty.
        if "no crontab" in (proc.stderr or "").lower():
            return ""
        raise CronError(f"crontab -l failed: {proc.stderr.strip() or proc.returncode}")
    return proc.stdout


def write_crontab(text: str) -> None:
    """Replace the current user's crontab with *text* (piped to ``crontab -``)."""
    import subprocess

    if text and not text.endswith("\n"):
        text += "\n"
    proc = subprocess.run(
        ["crontab", "-"],
        input=text,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise CronError(f"crontab - failed: {proc.stderr.strip() or proc.returncode}")


# --------------------------------------------------------------------------
# High-level operations (compose parsing + I/O)
# --------------------------------------------------------------------------


def _split(text: str) -> List[str]:
    return text.splitlines() if text else []


def install_line(
    line: str,
    *,
    reader: Optional[CrontabReader] = None,
    writer: Optional[CrontabWriter] = None,
) -> bool:
    """Append a managed *line* to the crontab idempotently.

    Returns True if the crontab was modified, False if an identical line was
    already present (so re-running ``schedule --install`` is a no-op). Never
    rewrites or drops the user's existing (hand-written or other) lines.
    """
    reader = reader or read_crontab
    writer = writer or write_crontab
    existing = _split(reader())
    if any(cur.strip() == line.strip() for cur in existing):
        return False
    existing.append(line)
    writer("\n".join(existing) + "\n")
    return True


def list_lines(*, reader: Optional[CrontabReader] = None) -> List[ManagedLine]:
    """Return all quackpack-managed crontab entries."""
    reader = reader or read_crontab
    return parse_managed(_split(reader()))


def remove_line(
    *,
    name: Optional[str] = None,
    index: Optional[int] = None,
    reader: Optional[CrontabReader] = None,
    writer: Optional[CrontabWriter] = None,
) -> List[ManagedLine]:
    """Remove quackpack-managed line(s), returning what was removed.

    Selection touches *only* managed lines: pass ``name`` to drop every managed
    entry for that query, or ``index`` to drop a single managed entry by its
    position in :func:`list_lines`. With neither, raises — we never bulk-wipe
    implicitly. User (non-managed) lines are always preserved verbatim.
    """
    if name is None and index is None:
        raise CronError("remove_line requires either name or index.")

    reader = reader or read_crontab
    writer = writer or write_crontab
    lines = _split(reader())
    managed = parse_managed(lines)

    to_remove: set[str] = set()
    removed: List[ManagedLine] = []
    if index is not None:
        match = [m for m in managed if m.index == index]
        if not match:
            raise CronError(f"No quackpack-managed schedule at index {index}.")
        removed = match
    else:
        removed = [m for m in managed if m.name == name]
        if not removed:
            raise CronError(f"No quackpack-managed schedule for query {name!r}.")
    to_remove = {m.raw.strip() for m in removed}

    kept = [ln for ln in lines if ln.strip() not in to_remove]
    writer("\n".join(kept) + ("\n" if kept else ""))
    return removed
