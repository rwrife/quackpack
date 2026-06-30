# quackpack demo

The animated demo in the top-level [`README`](../README.md) is **reproducible** —
it's generated from source in this folder, not hand-recorded. That means anyone
can regenerate an identical GIF, and CI can verify the underlying walkthrough
still works.

Three pieces, all kept in lock-step:

| File | Role |
| --- | --- |
| [`demo.sh`](./demo.sh) | The canonical walkthrough. Runs the real CLI against the bundled [`examples/`](../examples) dataset. Also the source of the CI smoke test (`tests/test_demo.py`). |
| [`demo.tape`](./demo.tape) | A [VHS](https://github.com/charmbracelet/vhs) tape that types the same commands and records an animated GIF. |
| [`cast2svg.py`](./cast2svg.py) | Tiny stdlib-only ANSI→SVG renderer that turns captured output into the static poster. |

Committed artifacts (regenerable from the above):

| File | What it is |
| --- | --- |
| `quackpack.svg` | The static "poster" still embedded in the README. |
| `quackpack.cast` | A recorded [asciinema](https://asciinema.org) cast (play it with `asciinema play demo/quackpack.cast`). |

## Regenerate the static poster (`quackpack.svg`)

No extra tools needed — it's pure stdlib:

```console
demo/demo.sh | python3 demo/cast2svg.py > demo/quackpack.svg
```

`cast2svg.py` understands the handful of ANSI color codes the CLI emits and lays
the output out on a monospace grid, so the box-drawing result tables stay aligned
in any SVG viewer (including GitHub).

## Re-record the cast (`quackpack.cast`)

```console
QP_DEMO_PACED=1 asciinema rec demo/quackpack.cast \
  --command "demo/demo.sh" --overwrite
```

The paced mode adds small pauses so playback reads naturally instead of flashing
by instantly.

## Render the animated GIF (optional: VHS)

[VHS](https://github.com/charmbracelet/vhs) renders a terminal GIF from a script,
headlessly and deterministically.

```console
# 1. Install quackpack so it's on PATH (the tape `Require`s it):
uv tool install .        # or: pipx install .

# 2. Install VHS (https://github.com/charmbracelet/vhs#installation), e.g.:
brew install vhs         # or `go install github.com/charmbracelet/vhs@latest`

# 3. From the repo root, render the GIF:
vhs demo/demo.tape       # writes demo/quackpack.gif
```

Re-running step 3 always yields the same animation, so refreshing the demo after
a UI tweak is a one-liner. Swap the README's `quackpack.svg` reference for
`quackpack.gif` once you've committed it.

Prefer to render the GIF from the cast instead? Use
[`agg`](https://github.com/asciinema/agg):

```console
agg demo/quackpack.cast demo/quackpack.gif
```

## Just want to see it run?

No recording tools needed — the walkthrough is a plain script:

```console
demo/demo.sh             # fast, for piping/CI
QP_DEMO_PACED=1 demo/demo.sh   # with pauses, as it looks when recorded
```

It stashes into a throwaway `QUACKPACK_HOME` (a temp dir) and cleans up after
itself, so it never touches your real `~/.quackpack` pack.
