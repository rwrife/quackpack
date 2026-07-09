"""Integration tests for ``export`` / ``import`` (backlog #5).

Drives the real Typer app against a throwaway ``QUACKPACK_HOME`` to cover the
command surface end to end:

* ``export`` — whole pack / by name / by ``--tag`` (AND), stdout vs ``-o FILE``,
  presets included but run history excluded, the dangling-ref warning, and the
  unknown-name / empty-selection exit codes;
* ``import`` — the ``skip`` / ``overwrite`` / ``rename`` strategies, provenance
  ``--tag``, stdin (``-``), the ``imported/skipped/renamed`` summary, and the
  malformed-file / bad-strategy / round-trip cases.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from quackpack.cli import app
from quackpack.store import Catalog

runner = CliRunner()


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("QUACKPACK_HOME", str(tmp_path))
    return tmp_path


def _add(name: str, sql: str, *extra: str) -> None:
    res = runner.invoke(app, ["add", "-n", name, "-q", sql, *extra])
    assert res.exit_code == 0, res.stdout + res.stderr


def _seed() -> None:
    _add("orders_clean", "select * from t where status <> 'void'", "--tags", "sales")
    _add(
        "big_orders",
        "select * from {{ orders_clean }} where amount > :n",
        "--tags",
        "sales,reports",
    )
    _add("misc_one", "select 1 as one", "--tags", "misc")
    runner.invoke(app, ["preset", "add", "big_orders", "q3-2026", "-p", "n=100"])


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------


def test_export_whole_pack_to_stdout() -> None:
    _seed()
    res = runner.invoke(app, ["export"])
    assert res.exit_code == 0, res.stdout + res.stderr
    doc = yaml.safe_load(res.stdout)
    assert doc["version"] == 1
    names = [q["name"] for q in doc["queries"]]
    assert set(names) == {"orders_clean", "big_orders", "misc_one"}


def test_export_includes_presets_excludes_history() -> None:
    _seed()
    # Give big_orders some run history that must NOT be exported.
    Catalog.load()  # ensure file exists
    cat = Catalog.load()
    cat.record_run("big_orders", "ok")
    res = runner.invoke(app, ["export", "big_orders"])
    assert res.exit_code == 0, res.stdout + res.stderr
    doc = yaml.safe_load(res.stdout)
    q = doc["queries"][0]
    assert q["presets"] == {"q3-2026": {"n": 100}}
    assert "run_count" not in q
    assert "last_run" not in q
    assert "last_status" not in q


def test_export_by_tag() -> None:
    _seed()
    res = runner.invoke(app, ["export", "--tag", "reports"])
    assert res.exit_code == 0, res.stdout + res.stderr
    doc = yaml.safe_load(res.stdout)
    assert [q["name"] for q in doc["queries"]] == ["big_orders"]


def test_export_name_and_tag_combine() -> None:
    _seed()
    # misc_one is not tagged sales, so the intersection is just orders_clean.
    res = runner.invoke(app, ["export", "orders_clean", "misc_one", "--tag", "sales"])
    assert res.exit_code == 0, res.stdout + res.stderr
    doc = yaml.safe_load(res.stdout)
    assert [q["name"] for q in doc["queries"]] == ["orders_clean"]


def test_export_to_file(tmp_path: Path) -> None:
    _seed()
    out = tmp_path / "share.yaml"
    res = runner.invoke(app, ["export", "orders_clean", "-o", str(out)])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert out.exists()
    doc = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert [q["name"] for q in doc["queries"]] == ["orders_clean"]
    # Human-facing confirmation goes to stderr, keeping stdout clean.
    assert "exported" in res.stderr


def test_export_warns_on_dangling_ref() -> None:
    _seed()
    # Exporting only big_orders leaves {{ orders_clean }} unresolved.
    res = runner.invoke(app, ["export", "big_orders"])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "warning" in res.stderr.lower()
    assert "orders_clean" in res.stderr


def test_export_unknown_name_fails() -> None:
    _seed()
    res = runner.invoke(app, ["export", "ghost"])
    assert res.exit_code == 1
    assert "No query named" in res.stderr


def test_export_empty_selection_is_success() -> None:
    _seed()
    res = runner.invoke(app, ["export", "--tag", "doesnotexist"])
    assert res.exit_code == 0, res.stdout + res.stderr
    doc = yaml.safe_load(res.stdout)
    assert doc["queries"] == []


# --------------------------------------------------------------------------
# import — round trip + strategies
# --------------------------------------------------------------------------


def _export_to(path: Path, *args: str) -> None:
    res = runner.invoke(app, ["export", *args, "-o", str(path)])
    assert res.exit_code == 0, res.stdout + res.stderr


def test_round_trip_into_fresh_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed()
    pack = tmp_path / "full.yaml"
    _export_to(pack)

    # Point at a brand-new home and import.
    fresh = tmp_path / "fresh_home"
    monkeypatch.setenv("QUACKPACK_HOME", str(fresh))
    res = runner.invoke(app, ["import", str(pack)])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "imported 3" in res.stdout

    cat = Catalog.load()
    assert set(cat.names()) == {"orders_clean", "big_orders", "misc_one"}
    # Presets survived the round trip exactly.
    assert cat.get("big_orders").presets == {"q3-2026": {"n": 100}}
    # And the SQL is byte-for-byte intact.
    assert cat.get("orders_clean").sql == "select * from t where status <> 'void'"


def test_import_skip_default_never_overwrites(tmp_path: Path) -> None:
    _seed()
    pack = tmp_path / "p.yaml"
    _export_to(pack, "misc_one")
    # Mutate the local misc_one so we can prove skip left it alone.
    runner.invoke(app, ["rm", "misc_one", "-y"])
    _add("misc_one", "select 'LOCAL' as v")

    res = runner.invoke(app, ["import", str(pack)])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "imported 0" in res.stdout
    assert "skipped 1" in res.stdout
    assert Catalog.load().get("misc_one").sql == "select 'LOCAL' as v"


def test_import_overwrite_replaces(tmp_path: Path) -> None:
    _seed()
    pack = tmp_path / "p.yaml"
    _export_to(pack, "misc_one")
    runner.invoke(app, ["rm", "misc_one", "-y"])
    _add("misc_one", "select 'LOCAL' as v")

    res = runner.invoke(app, ["import", str(pack), "--strategy", "overwrite"])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "imported 1" in res.stdout
    assert Catalog.load().get("misc_one").sql == "select 1 as one"


def test_import_rename_suffixes_collisions(tmp_path: Path) -> None:
    _seed()
    pack = tmp_path / "p.yaml"
    _export_to(pack, "misc_one")

    res = runner.invoke(app, ["import", str(pack), "--strategy", "rename"])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "renamed 1" in res.stdout
    names = set(Catalog.load().names())
    assert "misc_one" in names and "misc_one-2" in names


def test_import_tag_stamps_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed()
    pack = tmp_path / "p.yaml"
    _export_to(pack, "misc_one")
    fresh = tmp_path / "fresh"
    monkeypatch.setenv("QUACKPACK_HOME", str(fresh))

    res = runner.invoke(app, ["import", str(pack), "--tag", "from-alice"])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "from-alice" in Catalog.load().get("misc_one").tags


def test_import_from_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed()
    pack = tmp_path / "p.yaml"
    _export_to(pack)
    text = pack.read_text(encoding="utf-8")

    fresh = tmp_path / "fresh"
    monkeypatch.setenv("QUACKPACK_HOME", str(fresh))
    res = runner.invoke(app, ["import", "-"], input=text)
    assert res.exit_code == 0, res.stdout + res.stderr
    assert set(Catalog.load().names()) == {"orders_clean", "big_orders", "misc_one"}


# --------------------------------------------------------------------------
# import — error handling
# --------------------------------------------------------------------------


def test_import_missing_file_fails(tmp_path: Path) -> None:
    res = runner.invoke(app, ["import", str(tmp_path / "nope.yaml")])
    assert res.exit_code == 1
    assert "Could not read" in " ".join(res.stderr.split())


def test_import_bad_strategy_fails(tmp_path: Path) -> None:
    pack = tmp_path / "p.yaml"
    pack.write_text("version: 1\nqueries: []\n", encoding="utf-8")
    res = runner.invoke(app, ["import", str(pack), "-s", "bogus"])
    assert res.exit_code == 1
    assert "Unknown --strategy" in res.stderr


def test_import_malformed_yaml_fails(tmp_path: Path) -> None:
    pack = tmp_path / "bad.yaml"
    pack.write_text("not: a: pack\n", encoding="utf-8")
    res = runner.invoke(app, ["import", str(pack)])
    assert res.exit_code == 1
    assert "Malformed" in res.stderr


def test_import_no_queries_key_fails(tmp_path: Path) -> None:
    pack = tmp_path / "x.yaml"
    pack.write_text("version: 1\nfoo: bar\n", encoding="utf-8")
    res = runner.invoke(app, ["import", str(pack)])
    assert res.exit_code == 1
    # Rich may hard-wrap the message across lines on a long tmp path, so match
    # on whitespace-collapsed text.
    assert "no 'queries'" in " ".join(res.stderr.split())


def test_import_empty_pack_is_success(tmp_path: Path) -> None:
    pack = tmp_path / "empty.yaml"
    pack.write_text("version: 1\nqueries: []\n", encoding="utf-8")
    res = runner.invoke(app, ["import", str(pack)])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "imported 0" in res.stdout


def test_import_missing_arg_is_usage_error() -> None:
    # No FILE argument -> Click usage error, conventionally exit code 2.
    res = runner.invoke(app, ["import"])
    assert res.exit_code == 2
