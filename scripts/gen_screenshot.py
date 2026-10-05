#!/usr/bin/env python3
"""Regenerate docs/screenshot.png for the NixStore README.

This drives the real NixStore TUI headlessly with Textual's `Pilot` test
harness, captures the screen as an SVG, and converts it to the PNG shown in
the README. It exists so the marketing screenshot can be reproduced from
scratch instead of being a hand-captured, un-versioned artifact.

What it does
------------
1. Builds a throwaway "system flake" in a temp dir: a `flake.nix` that only
   pins `nixpkgs`, locked with `nix flake lock`, plus an empty
   `packages.json`. This is what NixStore searches against, and it means the
   screenshot is generated from a genuine nixpkgs package index rather than
   mock data.
2. Points a `nixstore.core.Config` at that temp flake (with its own temp
   cache dir) and runs the app via `NixStore(cfg, query="brave").run_test(...)`.
   It types the "brave" search, lets the index load, marks a package with
   Enter, sets a cosmetic `sub_title`, and calls `app.save_screenshot()`.
3. Converts the SVG to PNG with `rsvg-convert` (from librsvg).

Requirements
------------
- Run inside the dev shell so Textual and NixStore are importable and `nix`
  is on PATH:

      nix develop --command python3 scripts/gen_screenshot.py

- `nix` must be able to fetch / resolve `nixpkgs` (uses your local nix store
  and binary cache). The first run after a nixpkgs bump builds the package
  index with `nix search` (~30s); later runs reuse the temp cache for that
  invocation.
- `rsvg-convert` (package `librsvg`) must be on PATH for the PNG step. If it
  is not found, the script still writes the intermediate SVG and prints the
  manual conversion command:

      rsvg-convert -w 1260 -o docs/screenshot.png <tmp>/screenshot.svg

Usage
-----
    nix develop --command python3 scripts/gen_screenshot.py            # -> docs/screenshot.png
    nix develop --command python3 scripts/gen_screenshot.py OUT.png    # custom output path

Notes
-----
- No absolute /nix/store paths are hardcoded; `nix` and `rsvg-convert` are
  resolved via PATH (`shutil.which`).
- The terminal size (126x34) and the "brave" query match the committed
  screenshot. Exact pixel output can still drift with nixpkgs contents,
  Textual's theme, or font metrics, so treat this as "reproduces as closely
  as is reasonable" rather than byte-identical.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "screenshot.png"

# Match the committed screenshot.
TERMINAL_SIZE = (126, 34)
PNG_WIDTH = 1260  # rsvg-convert output width in px
SEARCH_QUERY = "brave"

# Minimal system flake: only pins nixpkgs, which is all NixStore searches.
TEMP_FLAKE_NIX = """\
{
  description = "throwaway flake for NixStore screenshot generation";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  outputs = { self, nixpkgs }: { };
}
"""


def require(tool: str) -> str:
    """Resolve a required executable via PATH or exit with a clear message."""
    path = shutil.which(tool)
    if path is None:
        sys.exit(
            f"error: '{tool}' not found on PATH. "
            "Run this inside `nix develop` and ensure the tool is installed."
        )
    return path


def build_temp_flake(workdir: Path) -> Path:
    """Create and lock a minimal nixpkgs-only flake; return its directory."""
    flake_dir = workdir / "flake"
    flake_dir.mkdir()
    (flake_dir / "flake.nix").write_text(TEMP_FLAKE_NIX)
    (flake_dir / "packages.json").write_text("[]\n")
    nix = require("nix")
    print("Locking temp flake (resolving nixpkgs)…", file=sys.stderr)
    subprocess.run(
        [nix, "flake", "lock",
         "--extra-experimental-features", "nix-command flakes",
         str(flake_dir)],
        check=True,
    )
    return flake_dir


async def capture_svg(cfg, svg_path: Path) -> None:
    """Drive the TUI with Pilot and save an SVG snapshot."""
    # Imported lazily so --help works outside the dev shell.
    from nixstore.tui import NixPackagesPanel, NixStore

    app = NixStore(cfg, query=SEARCH_QUERY)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        panel = app.query_one(NixPackagesPanel)
        # Let the package index load and the "brave" search populate.
        # load_index runs `nix search` on first use; give it generous time.
        for _ in range(120):
            await pilot.pause(0.5)
            if panel.packages:
                break
        # Re-run the search now that packages are loaded and settle the table.
        panel.run_search(SEARCH_QUERY)
        await pilot.pause(0.6)
        # Mark the highlighted package for install (shows the "+" marker).
        await pilot.press("enter")
        await pilot.pause(0.3)
        # Cosmetic sub_title for a clean screenshot caption.
        app.sub_title = "search · install · remove — one rebuild"
        await pilot.pause(0.2)
        app.save_screenshot(str(svg_path))


def convert_to_png(svg_path: Path, png_path: Path) -> bool:
    """Convert SVG -> PNG with rsvg-convert. Return True on success."""
    rsvg = shutil.which("rsvg-convert")
    if rsvg is None:
        print(
            "warning: 'rsvg-convert' (package librsvg) not found; "
            "SVG left in place.\n"
            f"  Convert manually: rsvg-convert -w {PNG_WIDTH} "
            f"-o {png_path} {svg_path}",
            file=sys.stderr,
        )
        return False
    png_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [rsvg, "-w", str(PNG_WIDTH), "-o", str(png_path), str(svg_path)],
        check=True,
    )
    return True


def main(argv: list[str]) -> int:
    output = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_OUTPUT

    from nixstore.core import Config

    with tempfile.TemporaryDirectory(prefix="nixstore-shot-") as tmp:
        workdir = Path(tmp)
        flake_dir = build_temp_flake(workdir)
        cfg = Config(
            flake=flake_dir,
            packages_file=flake_dir / "packages.json",
            cache_dir=workdir / "cache",
            modules_file=flake_dir / "modules.json",
            flake_file=flake_dir / "flake.nix",
        )
        svg_path = workdir / "screenshot.svg"
        asyncio.run(capture_svg(cfg, svg_path))
        print(f"Saved SVG: {svg_path}", file=sys.stderr)
        if convert_to_png(svg_path, output):
            print(f"Wrote PNG: {output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
