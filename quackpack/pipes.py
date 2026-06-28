"""Recent-pipe tracking for ``quackpack pipe``.

``pipe`` lets you run a throwaway query straight from stdin and *then* decide
whether it's worth keeping. The nudge — *"you've piped this a few times, stash
it?"* — needs a little memory of what you've run recently. That lives here, in a
single small JSON sidecar next to the catalog so it's:

* **separate from the catalog** — recent scratch queries are not saved queries;
  they shouldn't clutter (or risk corrupting) ``pack.yaml``.
* **bounded** — we keep only the last :data:`MAX_PIPES` entries, newest first,
  so the file can't grow without limit on a busy day.
* **pure + testable** — fingerprinting and the recency count are plain functions
  over data; the CLI just loads, asks, and saves.

File shape (``$QUACKPACK_HOME/pipes.json``)::

    {
      "version": 1,
      "pipes": [
        {"fingerprint": "…", "sql": "select …", "last": "2026-06-27T19:40:00+00:00", "count": 2},
        …
      ]
    }

A *fingerprint* is a normalised form of the SQL (whitespace collapsed, trailing
semicolons and case folded away) so cosmetically different spellings of the same
query — ``SELECT *  FROM t`` vs ``select * from t;`` — count as one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .history import now_iso
from .store import catalog_home

__all__ = [
    "PIPES_VERSION",
    "MAX_PIPES",
    "fingerprint",
    "PipeEntry",
    "PipeLog",
    "pipes_path",
]

PIPES_VERSION = 1

# How many recent pipes to remember. Plenty to power the "ran this a few times"
# nudge without letting the sidecar grow unbounded.
MAX_PIPES = 50

# Collapse any run of whitespace (incl. newlines) to a single space so layout
# differences don't fork a query's identity.
_WS_RE = re.compile(r"\s+")


def fingerprint(sql: str) -> str:
    """Return a stable identity for *sql*, ignoring cosmetic differences.

    Whitespace runs collapse to single spaces, surrounding whitespace and
    trailing semicolons are stripped, and the text is lower-cased. This is a
    deliberately *lightweight* normaliser — it is not a SQL parser — so two
    queries that differ only in formatting share a fingerprint while genuinely
    different SQL stays distinct.

    >>> fingerprint("SELECT *  FROM t ;")
    'select * from t'
    >>> fingerprint("select *\\nfrom t") == fingerprint("SELECT * FROM t")
    True
    """
    collapsed = _WS_RE.sub(" ", sql or "").strip()
    collapsed = collapsed.rstrip(";").strip()
    return collapsed.lower()


def pipes_path() -> Path:
    """Full path to the recent-pipe sidecar (``$QUACKPACK_HOME/pipes.json``)."""
    return catalog_home() / "pipes.json"


@dataclass
class PipeEntry:
    """One remembered pipe: its fingerprint, last SQL text, recency, and count."""

    fingerprint: str
    sql: str
    last: str = ""
    count: int = 0

    def to_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "sql": self.sql,
            "last": self.last,
            "count": self.count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PipeEntry":
        try:
            count = int(data.get("count", 0))
        except (TypeError, ValueError):
            count = 0
        return cls(
            fingerprint=str(data.get("fingerprint", "")),
            sql=str(data.get("sql", "")),
            last=str(data.get("last", "") or ""),
            count=count if count > 0 else 0,
        )


class PipeLog:
    """A bounded, newest-first log of recently piped queries.

    Like :class:`~quackpack.store.Catalog`, this owns its on-disk file: load with
    :meth:`load`, mutate with :meth:`record`, and call :meth:`save` to persist
    (the CLI does so only when a pipe actually ran, so a failed query doesn't
    pollute the nudge history).
    """

    def __init__(self, path: Optional[Path] = None, entries: Optional[list[PipeEntry]] = None) -> None:
        self.path = Path(path) if path is not None else pipes_path()
        self._entries: list[PipeEntry] = list(entries or [])

    # -- loading / saving --------------------------------------------------

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "PipeLog":
        """Load the sidecar, returning an empty log if it's absent or unreadable.

        The recent-pipe log is best-effort UX, never a source of truth, so a
        missing or corrupt file degrades to "no history" instead of raising.
        """
        p = Path(path) if path is not None else pipes_path()
        if not p.exists():
            return cls(path=p)
        try:
            raw = json.loads(p.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            return cls(path=p)
        items = raw.get("pipes") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return cls(path=p)
        entries = [PipeEntry.from_dict(it) for it in items if isinstance(it, dict)]
        return cls(path=p, entries=entries)

    def save(self) -> None:
        """Atomically write the log (newest first, capped at :data:`MAX_PIPES`)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": PIPES_VERSION,
            "pipes": [e.to_dict() for e in self._entries[:MAX_PIPES]],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path)

    # -- read --------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._entries)

    def find(self, sql: str) -> Optional[PipeEntry]:
        """Return the remembered entry matching *sql*'s fingerprint, if any."""
        fp = fingerprint(sql)
        for e in self._entries:
            if e.fingerprint == fp:
                return e
        return None

    def count_for(self, sql: str) -> int:
        """How many times *sql* has been piped before (0 if never seen)."""
        entry = self.find(sql)
        return entry.count if entry else 0

    # -- write -------------------------------------------------------------

    def record(self, sql: str, *, when: Optional[str] = None) -> PipeEntry:
        """Record one pipe of *sql*, moving it to the front and bumping its count.

        Returns the updated entry. The entry's ``count`` reflects the run we just
        recorded (so the *first* pipe yields ``count == 1``). The log is trimmed
        to :data:`MAX_PIPES` so it stays bounded.
        """
        fp = fingerprint(sql)
        stamp = when or now_iso()
        existing = None
        for i, e in enumerate(self._entries):
            if e.fingerprint == fp:
                existing = self._entries.pop(i)
                break
        if existing is None:
            entry = PipeEntry(fingerprint=fp, sql=sql.strip(), last=stamp, count=1)
        else:
            entry = PipeEntry(
                fingerprint=fp,
                sql=sql.strip(),  # remember the latest spelling
                last=stamp,
                count=existing.count + 1,
            )
        self._entries.insert(0, entry)
        del self._entries[MAX_PIPES:]
        return entry
