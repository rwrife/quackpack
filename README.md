# quackpack 🦆📦

**A personal pantry for your SQL.** Stash any ad-hoc DuckDB/SQLite query, give it a name,
tags, and `:params`, then rerun it instantly against any data file — no more re-typing that
gnarly one-liner you wrote three weeks ago.

It's like `tldr`/`navi` cheatsheets, but for *your own* analytical SQL. Your throwaway
queries become reusable tools without becoming things you have to maintain.

> Status: 🏗️ v0.1 in progress. See [PLAN.md](./PLAN.md) for the full roadmap.

## Why

DuckDB is the daily driver for slicing CSV/Parquet/SQLite — and every power user ends up
with a graveyard of great one-off queries lost in shell history and scratch files. The
engine is solved; the *workflow around it* is still messy. quackpack is the missing,
local-first, zero-server bit: **save the query, rerun the query.**

## The idea (preview)

```console
$ quackpack add --name top-errors --tags logs,triage \
    -q "SELECT path, count(*) c FROM read_parquet(:src) WHERE status >= 500 GROUP BY 1 ORDER BY c DESC LIMIT :n"

$ quackpack ls
  top-errors   [logs, triage]   last run: never

$ quackpack run top-errors --param src='logs/*.parquet' --param n=10
┏━━━━━━━━━━━━━━━━┳━━━━━┓
┃ path           ┃  c  ┃
┡━━━━━━━━━━━━━━━━╇━━━━━┩
│ /api/checkout  │ 412 │
│ /api/login     │  87 │
└────────────────┴─────┘
```

*(Interface is illustrative and may change during v0.1.)*

## Install

> Not published yet. Once v0.1 lands:
>
> ```console
> pipx install quackpack    # or: uv tool install quackpack
> ```

## Tech

Python · DuckDB · Typer · Rich · YAML. Boring on purpose.

## License

MIT (see `LICENSE`).

---

Part of an automated tool-lab experiment. Topic: `auto-tool-lab`.
