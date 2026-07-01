"""CI smoke test for the README demo (``demo/demo.sh``).

The animated demo in the README is generated from ``demo/demo.tape`` and driven
by ``demo/demo.sh``, which runs the *real* CLI against the bundled
``examples/`` dataset. This test executes that script end-to-end and asserts the
headline output still appears, so the demo (and therefore the recorded GIF the
README embeds) can't silently rot as the CLI evolves.

It is skipped gracefully where it can't run (no ``bash``, or DuckDB missing).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from quackpack import engine

# Repo root = two levels up from this test file (tests/ -> repo).
REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = REPO_ROOT / "demo"
DEMO_SH = DEMO_DIR / "demo.sh"
DEMO_TAPE = DEMO_DIR / "demo.tape"
DEMO_SVG = DEMO_DIR / "quackpack.svg"
DEMO_CAST = DEMO_DIR / "quackpack.cast"
CAST2SVG = DEMO_DIR / "cast2svg.py"

needs_bash = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)
needs_duckdb = pytest.mark.skipif(
    not engine.DUCKDB_AVAILABLE, reason="duckdb not installed"
)


def test_demo_assets_are_shipped() -> None:
    """The files that generate the README demo must exist."""
    assert DEMO_SH.is_file(), "demo/demo.sh is missing"
    assert DEMO_TAPE.is_file(), "demo/demo.tape is missing"
    assert (DEMO_DIR / "README.md").is_file(), "demo/README.md is missing"
    assert CAST2SVG.is_file(), "demo/cast2svg.py is missing"


def test_committed_demo_artifacts_exist() -> None:
    """The README embeds the SVG poster and links the asciinema cast; both must
    be committed so the README never shows a broken image/link."""
    assert DEMO_SVG.is_file(), "demo/quackpack.svg is missing"
    assert DEMO_CAST.is_file(), "demo/quackpack.cast is missing"


def test_poster_svg_is_well_formed_and_real() -> None:
    """The committed poster must be valid XML and contain genuine CLI output."""
    import xml.dom.minidom as minidom

    svg = DEMO_SVG.read_text(encoding="utf-8")
    minidom.parseString(svg)  # raises if malformed
    assert svg.lstrip().startswith("<svg")
    # Headline content from the real run should be baked into the still.
    # (Spaces render as &#160; in SVG <text>, so match single tokens.)
    assert "quackpack" in svg and "ls</text>" in svg
    assert "big-orders" in svg
    assert "600" in svg  # the :param-filtered big order


def test_cast_is_valid_asciinema_v2() -> None:
    """quackpack.cast must be a parseable asciinema v2 recording."""
    import json

    lines = DEMO_CAST.read_text(encoding="utf-8").splitlines()
    assert lines, "cast file is empty"
    header = json.loads(lines[0])
    assert header.get("version") == 2, "not an asciinema v2 cast"
    # Every event line must be valid JSON [time, type, data].
    saw_output = False
    for ln in lines[1:]:
        if not ln.strip():
            continue
        event = json.loads(ln)
        assert isinstance(event, list) and len(event) == 3
        if event[1] == "o" and "quackpack" in event[2]:
            saw_output = True
    assert saw_output, "cast never shows a quackpack command"


def test_demo_sh_is_executable() -> None:
    """demo/demo.sh should carry its executable bit so the docs' `demo/demo.sh`
    invocation works."""
    mode = DEMO_SH.stat().st_mode
    assert mode & stat.S_IXUSR, "demo/demo.sh is not executable (chmod +x)"


def test_demo_tape_matches_demo_commands() -> None:
    """The VHS tape must drive the same commands as demo.sh / the quickstart,
    so the recorded GIF can't drift from what actually runs."""
    tape = DEMO_TAPE.read_text(encoding="utf-8")
    for fragment in (
        "quackpack add -n top-regions",
        "quackpack ls",
        "quackpack run top-regions --file examples/sales.csv",
        "quackpack run big-orders --file examples/sales.csv --param min=300",
        "quackpack search region",
        "--format json",
    ):
        assert fragment in tape, f"demo.tape no longer types: {fragment!r}"


def _quackpack_on_path(env: dict[str, str]) -> dict[str, str]:
    """Guarantee a `quackpack` executable is resolvable for the subprocess.

    In CI the console script is installed (`pip install -e .`), but to keep this
    test robust everywhere, fall back to a tiny shim that invokes the CLI via the
    current interpreter when `quackpack` isn't already on PATH.
    """
    if shutil.which("quackpack", path=env.get("PATH")) is not None:
        return env

    shim_dir = Path(env["__QP_SHIM_DIR__"])
    shim = shim_dir / "quackpack"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'exec {sys.executable!r} -c '
        '"import sys; from quackpack.cli import app; app()" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


@needs_bash
@needs_duckdb
def test_demo_sh_runs_and_shows_headline_output(tmp_path: Path) -> None:
    """Running demo/demo.sh produces the documented walkthrough output."""
    env = dict(os.environ)
    env["__QP_SHIM_DIR__"] = str(tmp_path)
    # Keep recording pauses off so the smoke test is fast.
    env.pop("QP_DEMO_PACED", None)
    env = _quackpack_on_path(env)

    proc = subprocess.run(
        ["bash", str(DEMO_SH)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode == 0, (
        f"demo.sh exited {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )

    out = proc.stdout
    # The three stashes happened...
    assert "saved top-regions" in out
    assert "saved big-orders" in out
    assert "params: min" in out  # big-orders' :param was detected
    # ...the catalog listing rendered...
    assert "product-mix" in out
    # ...the CSV query produced the documented revenue figures...
    assert "995" in out and "east" in out
    # ...the :param-bound query filtered to the big orders...
    assert "600" in out and "gadget" in out
    # ...search recalled by metadata...
    assert "matches" in out
    # ...and JSON output was emitted.
    assert '"region": "east"' in out


@needs_bash
@needs_duckdb
def test_demo_sh_does_not_touch_real_pack(tmp_path: Path) -> None:
    """The demo must isolate itself in a throwaway QUACKPACK_HOME, never the
    caller's real pack."""
    fake_home = tmp_path / "real-home"
    fake_home.mkdir()

    env = dict(os.environ)
    env["__QP_SHIM_DIR__"] = str(tmp_path)
    # Point QUACKPACK_HOME at a dir we control; demo.sh should override it with
    # its own mktemp dir and leave this one untouched.
    env["QUACKPACK_HOME"] = str(fake_home)
    env.pop("QP_DEMO_PACED", None)
    env = _quackpack_on_path(env)

    proc = subprocess.run(
        ["bash", str(DEMO_SH)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr

    # demo.sh exported its own QUACKPACK_HOME, so our dir stays empty.
    assert not any(fake_home.iterdir()), (
        "demo.sh wrote into the caller's QUACKPACK_HOME instead of a temp dir"
    )


def test_cast2svg_renders_ansi_to_svg() -> None:
    """The ANSI->SVG renderer turns colored terminal text into valid SVG."""
    import importlib.util
    import xml.dom.minidom as minidom

    spec = importlib.util.spec_from_file_location("cast2svg", CAST2SVG)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # A bold-green prompt + a plain word, then a reset.
    sample = "\x1b[1;32m$\x1b[0m hello\nplain line\n"
    svg = mod.render(sample, title="t")
    minidom.parseString(svg)  # valid XML

    assert svg.startswith("<svg")
    assert "hello</text>" in svg
    assert "plain" in svg and "line</text>" in svg
    # The bold-green run should carry the palette color + bold weight.
    assert mod.PALETTE[32] in svg
    assert 'font-weight="bold"' in svg
