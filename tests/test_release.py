"""Release-readiness guards for the M6 ship.

These keep the three places a version lives — ``pyproject.toml``,
``quackpack.__version__``, and ``CHANGELOG.md`` — from drifting apart, which is
the classic way a tag-driven release goes out wrong. They're pure file/string
checks: fast, no install side effects.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import quackpack

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def test_package_version_matches_pyproject() -> None:
    """``quackpack --version`` (via ``__version__``) must match the package metadata."""
    assert quackpack.__version__ == _pyproject_version()


def test_changelog_documents_current_version() -> None:
    """CHANGELOG must carry a real, dated section for the version we'd ship."""
    version = _pyproject_version()
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    # e.g. "## [0.1.0] - 2026-06-28" — section header with an ISO-ish date.
    heading = re.compile(
        rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$",
        re.MULTILINE,
    )
    assert heading.search(changelog), (
        f"CHANGELOG.md is missing a dated '## [{version}] - YYYY-MM-DD' section; "
        "bump the changelog before tagging a release."
    )


def test_changelog_has_unreleased_section() -> None:
    """An ``Unreleased`` section should always exist for the next round of work."""
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(r"^## \[Unreleased\]$", changelog, re.MULTILINE)
