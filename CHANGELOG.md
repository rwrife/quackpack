# Changelog

All notable changes to **quackpack** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Param presets** (backlog #8): name a reusable set of `:param` values on a
  query with `preset add <query> <name> --param k=v` (values typed like
  `--param`, `key:type` hints supported), list them with `preset ls <query>`
  (also shown in `show`), and remove with `preset rm <query> <name>`. Replay a
  canned report in one keystroke via `run <query> --preset <name>`; explicit
  `--param` flags override the preset's values. Presets are stored alongside the
  query in the pack, so they travel with it.

### Changed

- **`--help` polish** (M6): command help is now rendered as Markdown, so inline
  code (`--file`, `:param`, `$EDITOR`, …) styles cleanly instead of leaking
  literal `` ``RST`` `` backticks. No behavior change.

### Documented

- **Exit-code contract** (M6): documented and test-pinned the convention — `0`
  on success (an empty `search`/`ls` result is success, not an error), `1` for
  runtime/user errors (uniform `error:` prefix), `2` for usage errors.

## [0.1.0] - 2026-06-28

First public release — the smallest genuinely useful "stash & rerun your SQL"
CLI. A stranger can `pipx install` it and run a stored query from the README in
about two minutes.

### Added

- **CLI scaffold** (M1): `quackpack` entry point built on Typer, `--version` /
  `-V`, the `hello` smoke-test command, packaging via `pyproject.toml`, and CI
  running `pytest` on Python 3.11 and 3.12.
- **Catalog store + CRUD** (M2): a single human-readable YAML pack under
  `~/.quackpack/` (override with `QUACKPACK_HOME`). `add` a query from `-q`, a
  `--file`, or stdin with `--name` / `--tags` / `--desc` (auto-detecting
  `:param` placeholders); `ls`, `show`, and `rm`.
- **Run engine** (M3): `run <name>` executes a stored query against a
  `--file` (CSV/Parquet/JSON/SQLite) or `--db` target via DuckDB, with a SQLite
  fallback. Output as a Rich `table` (default), `csv`, or `json`. `--engine`
  selects `auto` / `duckdb` / `sqlite`.
- **Parameters** (M4): typed `:param` binding via `--param key=value`
  (int/float/str coercion, explicit casts), interactive prompts for omitted
  params on a TTY, all bound through safe prepared statements.
- **Search, edit & history** (M5): `search <text>` matches any field
  (case-insensitive substring); `edit <name>` opens the SQL in `$EDITOR` and
  re-parses `:params` on save; every `run` records run history so `ls` / `show`
  surface "last run Nd ago" and the last outcome.
- **`pipe`** (backlog #7): run a throwaway query from stdin / `-q` / `--sql-file`
  with full `run`-parity, then offer to stash it — interactively, via
  `--save-as NAME` (with `--tags` / `--desc`), or never with `--no-save`. A
  fingerprinted recent-pipe log nudges you to save when you re-pipe the same SQL.
- **Examples quickstart**: a bundled `examples/` sample dataset (`sales.csv`),
  three starter queries, and a ready-to-load `pack.yaml`.
- **Docs**: README with quickstart, usage, and `pipx` / `uv tool` install docs.

[Unreleased]: https://github.com/rwrife/quackpack/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/rwrife/quackpack/releases/tag/v0.1.0
