#!/usr/bin/env python3
"""Render captured terminal output (with ANSI SGR colors) to a static SVG.

This produces ``demo/quackpack.svg`` — the still "poster" frame the README
embeds. It's a tiny, dependency-free renderer (stdlib only) that understands the
limited set of SGR escapes the demo emits: reset, bold, and the 8/16-color
foreground palette. That keeps the demo asset *real* (generated from actual CLI
output) and reproducible without pulling in image libraries.

Usage:
    demo/demo.sh | python3 demo/cast2svg.py > demo/quackpack.svg
    python3 demo/cast2svg.py < captured.txt --title "quackpack"

For the animated GIF, use VHS instead (see demo/README.md); this is the
lightweight fallback so the README always has a real image to show.
"""

from __future__ import annotations

import argparse
import html
import re
import sys

# A calm dark theme (Catppuccin-ish) so the SVG matches the VHS GIF vibe.
BG = "#1e1e2e"
FG = "#cdd6f4"
TITLEBAR = "#181825"
# 16-color palette indexed by SGR code (30-37 normal, 90-97 bright).
PALETTE = {
    30: "#45475a", 31: "#f38ba8", 32: "#a6e3a1", 33: "#f9e2af",
    34: "#89b4fa", 35: "#f5c2e7", 36: "#94e2d5", 37: "#bac2de",
    90: "#585b70", 91: "#f38ba8", 92: "#a6e3a1", 93: "#f9e2af",
    94: "#89b4fa", 95: "#f5c2e7", 96: "#94e2d5", 97: "#a6adc8",
}

SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")

# Layout (monospace grid).
CHAR_W = 8.4
LINE_H = 19.0
PAD_X = 16.0
PAD_TOP = 44.0  # room for the title bar
PAD_BOTTOM = 16.0
FONT_PX = 14


class Style:
    __slots__ = ("fg", "bold")

    def __init__(self) -> None:
        self.fg: str | None = None
        self.bold = False

    def copy(self) -> "Style":
        s = Style()
        s.fg = self.fg
        s.bold = self.bold
        return s


def _apply_sgr(style: Style, params: str) -> None:
    codes = [int(p) for p in params.split(";") if p != ""] or [0]
    for code in codes:
        if code == 0:
            style.fg = None
            style.bold = False
        elif code == 1:
            style.bold = True
        elif code == 22:
            style.bold = False
        elif code == 39:
            style.fg = None
        elif code in PALETTE:
            style.fg = PALETTE[code]


def _spans(line: str, style: Style):
    """Yield (text, Style) runs for one line, mutating `style` as we go."""
    pos = 0
    cur = style.copy()
    buf: list[str] = []
    for m in SGR_RE.finditer(line):
        if m.start() > pos:
            buf.append(line[pos : m.start()])
        if buf:
            yield "".join(buf), cur.copy()
            buf = []
        _apply_sgr(style, m.group(1))
        cur = style.copy()
        pos = m.end()
    if pos < len(line):
        buf.append(line[pos:])
    if buf:
        yield "".join(buf), cur.copy()


def render(text: str, title: str) -> str:
    # Strip a trailing newline so we don't draw a blank final row.
    lines = text.replace("\r\n", "\n").rstrip("\n").split("\n")
    cols = max((len(SGR_RE.sub("", ln)) for ln in lines), default=1)
    width = PAD_X * 2 + cols * CHAR_W
    height = PAD_TOP + len(lines) * LINE_H + PAD_BOTTOM

    out: list[str] = []
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}" '
        f'font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" '
        f'font-size="{FONT_PX}">'
    )
    out.append(f'<rect width="100%" height="100%" rx="10" fill="{BG}"/>')
    out.append(f'<rect width="100%" height="28" rx="10" fill="{TITLEBAR}"/>')
    out.append('<rect y="14" width="100%" height="14" fill="' + TITLEBAR + '"/>')
    # Traffic-light dots.
    for i, color in enumerate(("#f38ba8", "#f9e2af", "#a6e3a1")):
        out.append(f'<circle cx="{18 + i * 18}" cy="14" r="6" fill="{color}"/>')
    out.append(
        f'<text x="{width / 2:.0f}" y="18" fill="#9399b2" '
        f'text-anchor="middle" font-size="12">{html.escape(title)}</text>'
    )

    style = Style()
    y = PAD_TOP
    for ln in lines:
        x = PAD_X
        for text_run, st in _spans(ln, style):
            esc = html.escape(text_run)
            # Preserve leading/interior spaces.
            esc = esc.replace(" ", "&#160;")
            attrs = f'fill="{st.fg or FG}"'
            if st.bold:
                attrs += ' font-weight="bold"'
            out.append(
                f'<text x="{x:.1f}" y="{y:.1f}" {attrs} '
                f'xml:space="preserve">{esc}</text>'
            )
            x += len(text_run) * CHAR_W
        y += LINE_H
    out.append("</svg>\n")
    return "".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--title", default="quackpack 🦆📦 — stash & rerun your SQL")
    args = ap.parse_args(argv)
    data = sys.stdin.read()
    sys.stdout.write(render(data, args.title))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
