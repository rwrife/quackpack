"""Local named connections, deliberately separate from portable query packs."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from .store import catalog_home
from .targets import TargetError, parse_target


class Connections:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else catalog_home() / "connections.json"

    @staticmethod
    def _name(name: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise TargetError("Connection names must use letters, digits, dots, _ or -.")

    def load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise TargetError("Could not read connections.json; check its permissions and JSON.") from None
        if not isinstance(data, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in data.items()
        ):
            raise TargetError("Invalid connections.json: expected a name-to-DSN mapping.")
        for name, value in data.items():
            self._name(name)
            if parse_target(value).kind not in {"postgres", "mysql"}:
                raise TargetError("Named connections must use postgres:// or mysql://.")
        return data

    def _save(self, data: dict[str, str]) -> None:
        temporary: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".connections-")
            # mkstemp creates mode 0600 on POSIX. Windows inherits directory ACLs.
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2)
                stream.write("\n")
            os.replace(temporary, self.path)
        except OSError:
            raise TargetError("Could not save connections.json; check directory permissions.") from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def get(self, name: str) -> str:
        self._name(name)
        data = self.load()
        if name not in data:
            raise TargetError(f"No connection named {name!r}. Use 'quackpack conn add'.")
        return data[name]

    def add(self, name: str, dsn: str) -> None:
        self._name(name)
        if parse_target(dsn).kind not in {"postgres", "mysql"}:
            raise TargetError("Named connections must use postgres:// or mysql://.")
        data = self.load()
        if name in data:
            raise TargetError(f"Connection {name!r} already exists; remove it before replacing.")
        data[name] = dsn
        self._save(data)

    def remove(self, name: str) -> None:
        self._name(name)
        data = self.load()
        if name not in data:
            raise TargetError(f"No connection named {name!r}.")
        del data[name]
        self._save(data)
