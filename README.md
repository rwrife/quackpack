# quackpack 🦆📦

**A personal pantry for your SQL.** Stash any ad-hoc DuckDB/SQLite query, give it a name,
tags, and `:params`, then rerun it instantly against any data file — no more re-typing that
gnarly one-liner you wrote three weeks ago.

It's like `tldr`/`navi` cheatsheets, but for *your own* analytical SQL. Your throwaway
queries become reusable tools without becoming things you have to maintain.

> Status: 🏗️ v0.1 nearly done. **M5 (`search`, `edit`, run history) is live**, and
> there's now a bundled [`examples/`](./examples) quickstart you can run in ~2 minutes.
> See [PLAN.md](./PLAN.md) for the full roadmap.

## Why

DuckDB is the daily driver for slicing CSV/Parquet/SQLite — and every power user ends up
with a graveyard of great one-off queries lost in shell history and scratch files. The
engine is solved; the *workflow around it* is still messy. quackpack is the missing,
local-first, zero-server bit: **save the query, rerun the query.**

## Quickstart (≈2 minutes)

The repo ships a tiny sample dataset and three starter queries in
[`examples/`](./examples). From the repo root:

```console
# Use a throwaway pack so this never touches your real one:
$ export QUACKPACK_HOME="$(mktemp -d)"

# Stash the three starter queries (or load them all at once with
# `export QUACKPACK_HOME="$PWD/examples"` to use the bundled pack):
$ quackpack add -n top-regions -f examples/top-regions.sql --tags demo --desc "Revenue by region"
$ quackpack add -n big-orders  -f examples/big-orders.sql  --tags demo --desc "Orders at or above :min"
$ quackpack add -n product-mix -f examples/product-mix.sql --tags demo --desc "Units + revenue share by product"

# Run one against the sample CSV (the relation name is the file stem, `sales`):
$ quackpack run top-regions --file examples/sales.csv
┏━━━━━━━━┳━━━━━━━━━┳━━━━━━━┓
┃ region ┃ revenue ┃ units ┃
┡━━━━━━━━╇━━━━━━━━━╇━━━━━━━┩
│ east   │ 995     │ 15    │
│ south  │ 970     │ 19    │
│ west   │ 845     │ 13    │
│ north  │ 700     │ 11    │
└────────┴─────────┴───────┘
4 rows

# Bind a :param (or omit it to be prompted):
$ quackpack run big-orders --file examples/sales.csv --param min=300
```

More in [`examples/README.md`](./examples/README.md).

## Usage (available now)

Save a query — inline, from a file, or piped on stdin. Any `:param` placeholders are
detected and recorded automatically.

```console
$ quackpack add --name top-errors --tags logs,triage --desc "5xx by path" \
    -q "SELECT path, count(*) c FROM read_parquet(:src) WHERE status >= 500 GROUP BY 1 ORDER BY c DESC LIMIT :n"
saved top-errors  tags: logs, triage  params: src, n

$ cat report.sql | quackpack add --name monthly-revenue --tags finance
$ quackpack add --name quick -f ./queries/quick.sql
```

List, filter, inspect, and remove. `ls` shows when each query was **last run** (with an
`(error)` flag if the last run failed):

```console
$ quackpack ls
┏━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ name       ┃ tags         ┃ params ┃ last run ┃ description┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━┩
│ top-errors │ logs, triage │ src, n │ 2d ago   │ 5xx by path│
└────────────┴──────────────┴────────┴──────────┴────────────┘

$ quackpack ls --tag finance          # filter by tag
$ quackpack show top-errors           # SQL + metadata + run history (highlighted)
$ quackpack rm top-errors --yes       # remove (omit --yes to confirm)
```

### Find & edit

As your pack grows, `search` recalls a query by *anything* you remember about it — it
substring-matches (case-insensitively) across name, SQL body, description, and tags:

```console
$ quackpack search 5xx           # matches the description
$ quackpack search read_parquet  # matches the SQL body
$ quackpack search triage        # matches a tag
┏━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ name       ┃ tags         ┃ params ┃ last run ┃ description┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━┩
│ top-errors │ logs, triage │ src, n │ 2d ago   │ 5xx by path│
└────────────┴──────────────┴────────┴──────────┴────────────┘
1 match
```

Tweak a stored query in your editor — quackpack opens the SQL in `$EDITOR` (or `--editor`)
and **re-detects `:params` on save**, so adding or removing a placeholder just works:

```console
$ quackpack edit top-errors       # opens $VISUAL / $EDITOR (vi by default)
updated top-errors  params: src, n, since
```

### Run a query

`quackpack run <name>` executes a saved query against a data target and renders the
results. Point it at a file with `--file` (CSV / Parquet / SQLite) or a database with
`--db` (DuckDB or SQLite). DuckDB is the default engine; pass `--engine sqlite` to force
the dependency-free fallback.

```console
# Reference a --file by its stem (sales.csv -> the `sales` relation):
$ quackpack add -n top-regions -q "SELECT region, sum(amount) AS total FROM sales GROUP BY 1 ORDER BY total DESC"
$ quackpack run top-regions --file sales.csv
┏━━━━━━━━┳━━━━━━━┓
┃ region ┃ total ┃
┡━━━━━━━━╇━━━━━━━┩
│ east   │   250 │
│ west   │   175 │
└────────┴───────┘
2 rows

# Same query, a Parquet file, piped out as CSV:
$ quackpack run top-regions --file sales.parquet --format csv

# Bind :params and emit JSON for jq/scripts:
$ quackpack add -n big -q "SELECT * FROM sales WHERE amount > :min ORDER BY amount"
$ quackpack run big --file sales.csv --param min=90 --format json

# Omit a param and quackpack prompts for it (when run in a terminal):
$ quackpack run big --file sales.csv
param min: 90
┏━━━━━━━━┳━━━━━━━━┓
┃ region ┃ amount ┃
┡━━━━━━━━╇━━━━━━━━┩
│ west   │    100 │
│ east   │    250 │
└────────┴────────┘

# Query tables inside a database file (DuckDB or SQLite):
$ quackpack run big --db shop.duckdb --param min=90
$ quackpack run big --db shop.sqlite --param min=90 --engine sqlite
```

Notes:

- **Relation name** = the data file's stem, sanitised (`my data.csv` → `my_data`). You can
  also use DuckDB table functions directly, e.g. `... FROM read_csv_auto('sales.csv')` or
  `read_parquet('data/*.parquet')`.
- **`--param key=value`** is repeatable and bound via the driver's native prepared
  statements (no string interpolation). quackpack stores `:name` placeholders and
  translates them to DuckDB's `$name` automatically. Values are **typed automatically** as
  `int` / `float` / `str` so numeric filters compare numerically; force a type with a
  `key:type` hint, e.g. `--param zip:str=00501` (keep the leading zero) or
  `--param n:int=5`. **Any declared param you don't pass is prompted for interactively**
  when running in a terminal; in pipes/CI it just warns so nothing hangs.
- **`--format`**: `table` (default), `csv`, or `json`.
- **`--engine`**: `auto` (DuckDB if installed, else SQLite), `duckdb`, or `sqlite`. The
  SQLite fallback ingests CSVs (inferring numeric columns) and reads `.sqlite` files;
  Parquet requires DuckDB.

### Where queries live

A single human-readable, diffable YAML file at `~/.quackpack/pack.yaml`. Set
`QUACKPACK_HOME` to relocate it (e.g. point it inside a git repo to version your pack):

```yaml
version: 1
queries:
  - name: top-errors
    sql: SELECT path, count(*) c FROM read_parquet(:src) WHERE status >= 500 ...
    tags: [logs, triage]
    desc: 5xx by path
    created: "2026-06-22T19:44:07+00:00"
    params: [src, n]
    run_count: 4
    last_run: "2026-06-25T19:40:00+00:00"
    last_status: ok
```

Every `quackpack run` bumps `run_count` / `last_run` / `last_status`, which is what powers
the "last run" column in `ls` and the run summary in `show`.

## Coming next (finishing M6)

Discovery and recall are done (M5: `search`, `edit`, run history) and the
[`examples/`](./examples) quickstart has landed. Remaining for the v0.1 ship:
an asciinema/GIF demo and a tagged `v0.1.0` release (optionally on PyPI).

## Install

From source today (PyPI publish lands with `v0.1.0`):

```console
# With uv (recommended):
uv tool install git+https://github.com/rwrife/quackpack

# Or with pipx:
pipx install git+https://github.com/rwrife/quackpack

# Or a plain editable dev install:
git clone https://github.com/rwrife/quackpack && cd quackpack
pip install -e ".[dev]"
```

Once v0.1 is published:

```console
pipx install quackpack    # or: uv tool install quackpack
```

## Tech

Python · DuckDB · Typer · Rich · YAML. Boring on purpose.

## License

MIT (see `LICENSE`).

---

Part of an automated tool-lab experiment. Topic: `auto-tool-lab`.
