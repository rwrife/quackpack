# Changelog

All notable changes to **quackpack** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`schedule` — emit (and optionally install) a cron line** (backlog #11 / issue #33):
  `quackpack schedule <name> --at "0 8 * * *" --file data.parquet --out out.csv` prints a
  ready-to-paste crontab line that reruns a saved query via `quackpack run` (always with
  `--no-input`/`--no-snapshot`) and redirects the result to a file — no daemon, no server.
  `--format`, `--param`, `--preset`, and `--file`/`--db` thread through exactly like `run`,
  shell-quoted for safe round-tripping. `--install` appends the line to your crontab
  **idempotently** behind a `--yes`/confirm guard, tagging each managed line with a
  `# quackpack:schedule <name>` sentinel; `--list` and `--remove` operate on **only**
  quackpack's own lines, never clobbering existing crontab entries. Cron-expression
  validation, pure command/line building, and mocked crontab mutation are unit-tested;
  README documents the emit-a-line philosophy and install caveats.
- **Glob / multi-file fan-out on `run`** (backlog #7 / issue #32): pass a glob to
  `--file` (e.g. `quackpack run daily-errors --file 'logs/*.parquet'`) and quackpack expands
  it and runs the stored query across every matching file. Results are `UNION ALL`ed into one
  table by default; `--per-file` renders a separate labelled table per file, and
  `--with-source` prepends a `_source_file` provenance column (both modes). In glob mode each
  file is exposed under the stable relation name `data` so a single `select * from data`
  query runs unchanged across files with differing stems. Works for CSV/Parquet/JSON; a
  zero-match glob is a clean error, mismatched UNION schemas fail loudly (use `--per-file`),
  and `--format csv/json` output paths are unchanged. README documents glob usage and
  shell-quoting caveats.
- **`explain` — query plan + static lints** (backlog #12 / issue #31): `quackpack explain
  <name> [--file/--db ...] [--param ...]` renders DuckDB's `EXPLAIN` plan for a saved query
  without keeping its results — a fast, read-only feasibility/perf check before you commit a
  query to your library. `--analyze` switches to `EXPLAIN ANALYZE` (actually executes and
  reports timings). Cheap static lints surface common footguns to stderr (non-fatal): a
  broad `SELECT *` projection and unfiltered full-file scans (`--no-lint` silences them).
  Params bind exactly like `run` (presets, interactive prompts, and `--no-input` honoured),
  and the SQLite fallback degrades gracefully to `EXPLAIN QUERY PLAN`. README documents the
  command.
- **Agent / MCP tool manifest** (backlog #6): quackpack now exposes your pack as callable
  tools for an agent / MCP layer, without becoming an always-on server. `quackpack tools
  [--tag T] [--format json|jsonschema]` prints a manifest of every saved query with its
  `--desc` and a params schema derived from its `:param` placeholders (name, `int`/`float`/
  `str` type matching the run engine's coercions, required flag, and any preset-provided
  default); `jsonschema` emits JSON-Schema `inputSchema` objects. `quackpack describe <name>
  [--format ...]` returns the single-tool entry for lazy per-tool discovery. The `run`
  command gains a non-interactive contract for agents/CI: `--no-input` (or
  `QUACKPACK_NO_INPUT=1`) never prompts — a missing required param exits `1` with a clean
  `error:` on stderr — and with `--format json` it (or `--envelope`) emits the documented
  `{"columns": [...], "rows": [...], "rowcount": N}` envelope; plain `--format json` keeps the
  classic array-of-objects shape. An illustrative, dependency-light `examples/mcp_shim.py`
  shows wiring the manifest + run to an MCP server (demonstration, not a runtime dep). README
  gains an "Agent / MCP integration" section.
- **`last` — recall the cached result** (backlog #8): `quackpack last <name>` re-shows the
  result cached by the previous `run`, straight from the snapshot — **no engine spin-up, no
  data-file access**. It's the companion to `run`/`diff`: `run` computes, `diff` compares,
  `last` remembers. A provenance header notes the capture age (`cached Nd ago`) and the
  params it ran with, so a stale cache is never mistaken for a fresh run; for `--format
  csv|json` that header goes to stderr so stdout stays clean for piping. `--file`/`--db`/
  `--param` are irrelevant to a cache read and are rejected as a usage error, and a missing
  snapshot exits `1` with `error: no cached result for '<name>' — run it first`.
- **Export / import & sharing packs** (backlog #5): `export [NAMES...] [--tag T] [-o FILE]`
  writes a curated selection of queries — **plus their presets and metadata, but not run
  history or cached snapshots** — as a standalone pack file (stdout by default, so it pipes
  to a gist). Name args and `--tag` combine (AND); with neither, the whole pack is exported.
  It warns (non-fatally) when a selected query has a `{{ ref }}` to one left out.
  `import FILE [--strategy skip|overwrite|rename] [--tag T]` merges an exported pack (or a
  whole `pack.yaml`; `-` reads stdin) into your library: the default `skip` never
  overwrites, `overwrite` replaces same-name queries, and `rename` imports collisions as
  `name-2`. `--tag from-alice` stamps provenance on everything imported, and a summary
  reports `imported / skipped / renamed`. Round-trip safe: `export` then `import` into a
  fresh `QUACKPACK_HOME` reproduces the queries and presets exactly. Local-first — no
  server, no accounts.
- **Result snapshots & diff** (backlog #3): every successful `run` now caches its
  result, and `diff <name>` re-runs the query to show what changed since that
  cached run — rows **added**, rows **removed**, and (when you record identity
  columns with `run --key <col>`) rows whose values **changed**, column by column
  (`old → new`). Without a key, rows are matched on whole-row identity
  (multiset-aware). `diff` takes the same targeting flags as `run`
  (`--file`/`--db`, `--param`, `--preset`, `--engine`), plus `--key` to override
  the recorded identity and `--update` to re-baseline the snapshot as you go.
  `run --no-snapshot` opts out of caching; `snapshot show <name>` inspects the
  cached result and `snapshot rm <name>` clears it. Snapshots live in
  `~/.quackpack/snapshots/` as one small JSON per query, separate from the
  catalog. A lightweight data-drift / regression spot check — not BI.

## [0.1.0] - 2026-07-05

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
- **Param presets** (backlog #8): name a reusable set of `:param` values on a
  query with `preset add <query> <name> --param k=v` (values typed like
  `--param`, `key:type` hints supported), list them with `preset ls <query>`
  (also shown in `show`), and remove with `preset rm <query> <name>`. Replay a
  canned report in one keystroke via `run <query> --preset <name>`; explicit
  `--param` flags override the preset's values. Presets are stored alongside the
  query in the pack, so they travel with it.
- **Query templating / composition** (backlog #10): reference one saved query
  inside another with `{{ other_query }}`. References are inlined as
  parenthesised subqueries at `run` time and resolved to a single flat SQL
  string before execution (params from the referenced query are bound too);
  cycles (direct or transitive) and unknown references fail with a clean
  `error:`. `show` lists a query's `references`, and `show --expanded <name>`
  previews the fully flattened SQL. Lets you factor common cleaning/joins into
  reusable building blocks.
- **`--help` polish** (M6): command help is rendered as Markdown, so inline
  code (`--file`, `:param`, `$EDITOR`, …) styles cleanly instead of leaking
  literal RST backticks.
- **Docs**: README with quickstart, usage, and `pipx` / `uv tool` install docs.
- **Exit-code contract** (M6): documented and test-pinned the convention — `0`
  on success (an empty `search`/`ls` result is success, not an error), `1` for
  runtime/user errors (uniform `error:` prefix), `2` for usage errors.

[Unreleased]: https://github.com/rwrife/quackpack/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/rwrife/quackpack/releases/tag/v0.1.0
