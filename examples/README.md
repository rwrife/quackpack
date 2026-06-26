# quackpack examples 🦆📦

A tiny sample dataset and three starter queries so you can go from
`pip install` to a real result in about two minutes.

- **`sales.csv`** — 15 fake orders (`order_date, region, product, units, amount`).
- **`top-regions.sql`** — total revenue by region.
- **`big-orders.sql`** — orders at or above a `:min` you pass at run time.
- **`product-mix.sql`** — units + revenue share per product.

## 2-minute quickstart

From the repo root (so the `examples/` paths resolve), point quackpack at a
throwaway home so this never touches your real pack:

```console
$ export QUACKPACK_HOME="$(mktemp -d)"

# Stash the three starter queries:
$ quackpack add -n top-regions -f examples/top-regions.sql --tags demo --desc "Revenue by region"
$ quackpack add -n big-orders  -f examples/big-orders.sql  --tags demo --desc "Orders at or above :min"
$ quackpack add -n product-mix -f examples/product-mix.sql --tags demo --desc "Units + revenue share by product"

# ...or load all three at once from the bundled pack:
$ export QUACKPACK_HOME="$PWD/examples"

# See what's in the pack:
$ quackpack ls

# Run them against the sample CSV (the relation name is the file stem: `sales`):
$ quackpack run top-regions --file examples/sales.csv
$ quackpack run big-orders  --file examples/sales.csv --param min=300
$ quackpack run product-mix --file examples/sales.csv
```

Expected output for `top-regions`:

```
┏━━━━━━━━┳━━━━━━━━━┳━━━━━━━┓
┃ region ┃ revenue ┃ units ┃
┡━━━━━━━━╇━━━━━━━━━╇━━━━━━━┩
│ east   │ 995     │ 15    │
│ south  │ 970     │ 19    │
│ west   │ 845     │ 13    │
│ north  │ 700     │ 11    │
└────────┴─────────┴───────┘
4 rows
```

## Things to try next

- Omit the param to get prompted: `quackpack run big-orders --file examples/sales.csv`
- Pipe a query out for `jq`/scripts: `quackpack run product-mix --file examples/sales.csv --format json`
- Edit a query and watch params re-detect: `quackpack edit big-orders`
- Recall by anything you remember: `quackpack search revenue`

> Heads up: `examples/pack.yaml` is a curated, version-pinned copy of these
> queries for the one-shot load above. Your day-to-day pack lives at
> `~/.quackpack/pack.yaml` (override with `QUACKPACK_HOME`).
