#!/usr/bin/env python3
"""
Generate PNG icons from icon.svg.
Requires: cairosvg  (pip install cairosvg)
      or: Inkscape  (inkscape --export-type=png ...)
      or: ImageMagick (convert ...)

Run from the browser_extension/icons/ directory:
  python3 generate_icons.py
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SVG  = HERE / "icon.svg"

SIZES = [16, 48, 128]


def try_cairosvg():
    import cairosvg  # type: ignore
    for size in SIZES:
        cairosvg.svg2png(url=str(SVG),
                         write_to=str(HERE / f"icon{size}.png"),
                         output_width=size, output_height=size)
    return True


def try_inkscape():
    for size in SIZES:
        r = subprocess.run([
            "inkscape", "--export-type=png",
            f"--export-width={size}", f"--export-height={size}",
            f"--export-filename={HERE / f'icon{size}.png'}",
            str(SVG),
        ], capture_output=True)
        if r.returncode != 0:
            return False
    return True


def try_imagemagick():
    for size in SIZES:
        r = subprocess.run([
            "convert", "-background", "none",
            "-resize", f"{size}x{size}", str(SVG),
            str(HERE / f"icon{size}.png"),
        ], capture_output=True)
        if r.returncode != 0:
            return False
    return True


for fn in (try_cairosvg, try_inkscape, try_imagemagick):
    try:
        if fn():
            print(f"Icons generated: {[f'icon{s}.png' for s in SIZES]}")
            sys.exit(0)
    except Exception as e:
        print(f"  {fn.__name__} failed: {e}", file=sys.stderr)

print("ERROR: Could not generate icons. Install cairosvg, Inkscape, or ImageMagick.",
      file=sys.stderr)
sys.exit(1)
