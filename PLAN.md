# quackpack 🦆📦

> A personal pantry for your SQL. Stash any ad-hoc query, give it a name, tags, and
> `:params`, then rerun it instantly against any data file — no more re-typing that
> gnarly DuckDB one-liner from three weeks ago.

## 1. Pitch

Every DuckDB/SQLite power user accumulates a graveyard of brilliant one-off queries that
vanish into shell history, scratch files, and Slack messages. **quackpack** is a tiny
local CLI that turns those throwaway queries into a searchable, taggable, *parameterized*
library you can rerun in two keystrokes. Think `navi`/`tldr` cheatsheets, but for *your
own* analytical SQL instead of shell commands — your queries become reusable tools without
becoming maintenance burdens.

## 2. Trend inspiration

- **"DuckDB solved the query engine. The workflow around it is still messy"** — Medium /
  HN, June 2026. Core quote: *"stop turning every useful DuckDB query into a small custom
  tool that somebody has to maintain."* That tension — queries good enough to keep, too
  small to productize — is exactly quackpack's wedge.
  <https://slotix.medium.com/duckdb-solved-the-query-engine-the-workflow-around-it-is-still-messy-90c314e66dbd>
- **DuckDB Internals (HN, id=48553388)** — top comment: *"DuckDB has been probably my most
  used tool in 2026… incredible at quickly prototyping and slicing / dicing data."* Huge,
  growing daily-driver base = lots of orphan queries.
  <https://news.ycombinator.com/item?id=48553388>
- **"10 DuckDB UI Workflows That Feel Truly Pro"** — recommends a hand-rolled `queries/`
  folder of named SQL files. People are clearly improvising this pattern by hand; quackpack
  makes it a real, ergonomic tool.
  <https://medium.com/@bhagyarana80/10-duckdb-ui-workflows-that-feel-truly-pro-6ea8a79362d9>
- **DuckDB CLI can't easily bind params** — open feature request asking for a
  `duckdb_parameters` table like SQLite's. Parameter binding from the CLI is genuinely
  awkward today; quackpack owns the `:param` ergonomics on top.
  <https://github.com/duckdb/duckdb/discussions/12626>
- **The TUI / "small sharp tools" renaissance** (Terminal Trove new tools, June 2026:
  redthread pinboard, bttf datetime CLI, Slumber HTTP client). Tiny single-purpose,
  local-first dev tools are having a moment. quackpack fits the genre.
  <https://terminaltrove.com/new/>

## 3. Why it's different

- **vs `duckdbsnippets.com`** — that's a *community website* of generic snippets you copy
  by hand. quackpack is *your* private, local library of *your* queries, runnable in place.
- **vs a hand-rolled `queries/*.sql` folder** — no naming convention discipline required,
  no `cat file.sql | duckdb` plumbing. quackpack adds search, tags, params, run history,
  and last-result caching out of the box.
- **vs DuckDB / SQLite CLI history (`.read`, up-arrow)** — shell history is unsearchable
  by intent, unparameterized, and evaporates across machines. quackpack stores queries with
  human metadata and round-trips through a single portable file.
- **vs BI tools (Metabase, Superset, Mode)** — those are heavy servers for dashboards.
  quackpack is a zero-server, single-binary-feel CLI for the *individual* analyst's muscle
  memory. It's a subset on purpose: just the "save & rerun my query" 5%.
- **vs `schema-seance` (our own repo)** — that profiles files for schema/PII/anomalies.
  quackpack manages and reruns *queries*. Zero overlap; they're actually complementary
  (seance to explore, quackpack to remember).
- **vs `navi`/`pet`/`tldr`** — those are shell-command cheatsheets. quackpack is
  SQL-native: it knows about engines, params, and result rendering, not just text snippets.

## 4. MVP scope (v0.1)

The smallest genuinely useful thing:

- `quackpack add` — save a query (from `-q`, a file, or stdin) with `--name`, `--tags`,
  optional `--desc`. Auto-detects `:param` placeholders.
- `quackpack ls` — list saved queries (name, tags, desc, last-run age). `--tag` filter.
- `quackpack show <name>` — print the stored SQL + metadata.
- `quackpack run <name>` — execute against a target (`--db file.duckdb` / `--file data.csv`
  / `--file data.parquet`). Supply params via `--param key=value` (prompt interactively if
  missing). Render results as a clean table (or `--format csv/json`).
- `quackpack rm <name>` / `quackpack edit <name>` (opens `$EDITOR`).
- `quackpack search <text>` — fuzzy/substring match over name + body + tags + desc.
- Storage: a single human-readable file (`~/.quackpack/pack.yaml` or a small SQLite
  catalog) so it's portable, diffable, and git-friendly.
- Engine: DuckDB by default (handles CSV/Parquet/JSON/SQLite natively); SQLite fallback.

## 5. Tech stack

Boring, fast, batteries-included:

- **Python 3.11+** — the DuckDB ecosystem's lingua franca; trivial to ship and extend.
- **DuckDB (`duckdb` pip package)** — the engine. Reads CSV/Parquet/JSON/SQLite with zero
  extra deps and gives us prepared-statement param binding for free.
- **Typer** (Click under the hood) — ergonomic, typed CLI with near-zero boilerplate.
- **Rich** — gorgeous result tables, syntax-highlighted SQL in `show`, nice `ls`.
- **PyYAML** — human-readable, diffable catalog file. (Swap to SQLite catalog only if the
  flat file gets slow — unlikely for a personal library.)
- **pytest** — tests.
- **pipx / `uv`-installable** — single-command install; `quackpack` on PATH.

Rationale: every piece is mature, popular, and keeps v0.1 to a few hundred lines. No web
server, no build step, no native toolchain.

## 6. Architecture

```
quackpack/
  cli.py          # Typer app, command wiring, arg parsing
  store.py        # load/save the YAML catalog; Query dataclass; CRUD
  engine.py       # DuckDB/SQLite execution, file attachment, param binding
  params.py       # detect :params, merge --param + interactive prompts
  render.py       # Rich tables / csv / json output; SQL syntax highlight
  history.py      # per-query run log (count, last_run, last_status)
  __init__.py
tests/
```

Key modules:

- **store** — owns the catalog file. A `Query` = `{name, sql, tags[], desc, created,
  params[]}`. CRUD + search live here.
- **engine** — the only place that touches DuckDB. Given a query + target + bound params,
  attaches the file/db, runs the prepared statement, returns rows + columns.
- **params** — regex-extracts `:name` placeholders, reconciles them with `--param` flags,
  prompts for the rest. Pure functions, easy to test.
- **render** — output strategies; default Rich table, plus `csv`/`json` for piping.
- **history** — lightweight append log so `ls` can show "last run 3d ago" and `run` can
  bump counters.

## 7. Milestones

1. **M1 — Scaffold + hello-world.** Repo layout, Typer app, packaging (`pyproject.toml`),
   `quackpack --version` and `quackpack hello` work; CI runs `pytest` on an empty suite.
2. **M2 — Catalog store + add/ls/show/rm.** YAML-backed CRUD; `add` from `-q`/file/stdin;
   `ls`/`show`/`rm`; tests for store roundtrip.
3. **M3 — Run engine (DuckDB).** Execute a stored query against `--db`/`--file`
   (CSV/Parquet/SQLite); Rich table output; SQLite fallback engine.
4. **M4 — Parameters.** Detect `:param`s, bind via `--param`, interactively prompt for
   missing ones, type-coerce; safe prepared statements.
5. **M5 — Search + history + edit.** Fuzzy/substring `search`; `$EDITOR` `edit`; run
   history powering "last run" + run counts; `--format csv/json`.
6. **M6 — Polish + ship v0.1.** README with GIF/asciinema, `pipx`/`uv` install docs,
   examples, tagged `v0.1.0` release.

## 8. Backlog / future features (v0.2+)

1. **`quackpack pipe`** — read SQL on stdin, run, and offer to save if it's a keeper
   ("you ran this twice this week, stash it?").
2. **Param presets / saved bindings** — name a set of param values (e.g. `--preset q3-2026`).
3. **Result snapshots & diff** — cache last result; `quackpack diff <name>` shows what
   changed since the last run (great for data-drift spot checks).
4. **Templating / composition** — reference one saved query as a CTE/subquery inside
   another (`{{ orders_clean }}`), building a mini query library.
5. **`quackpack export/import`** — share a curated pack as a single file or gist; team packs.
6. **Shell + agent integration** — emit JSON schema so AI agents/MCP can discover and run
   your saved queries as tools (ties into the MCP wave without being yet-another-MCP-server).
7. **Run on a glob / multi-file** — `--file 'logs/*.parquet'` fan-out + UNION.
8. **`quackpack last`** — re-show the most recent result without re-running.
9. **Postgres/MySQL targets** via DuckDB extensions or connection strings.
10. **Lightweight TUI** (`quackpack tui`) — browse, preview, run with Textual.
11. **Scheduling hooks** — print a cron line to run a query + dump to file/Slack.
12. **Lint/explain** — `quackpack explain <name>` shows DuckDB's query plan; warn on
    `SELECT *` against wide files.

## 9. Out of scope (deliberately NOT building)

- A query *editor*/IDE, autocomplete, or LSP — use your editor; we just store & run.
- A hosted service, accounts, sync server, or web dashboard — local-first, single file.
- A BI/visualization layer (charts, dashboards) — that's Metabase/Superset territory.
- Write/DDL workflow management or migrations — quackpack is for *reads/analytics*.
- Multi-user permissions, RBAC, audit — it's a personal tool.
- Reimplementing DuckDB features — we orchestrate the engine, we don't fork it.
