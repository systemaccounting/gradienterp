#!/usr/bin/env python3
"""Generate gradientERP-lockup.svg — the font-independent wordmark lockup.

The SVG is a build OUTPUT of this script: every glyph is outlined to a <path> from
SF Pro (so it renders identically on every device, no SF/Georgia substitution). The ∇
reuses the extracted path from gradientERP-nabla.svg.

Usage:
    python3 build-lockup.py [baseline] [x0]
        baseline  wordmark baseline y   (lower => wordmark sits HIGHER in the ∇)   default 167
        x0        wordmark start x      (lower => wordmark sits LEFT, more into ∇)  default 126

Writes the SVG to both the canonical art dir and the web client:
    docs/images/art/gradientERP-lockup.svg
    prod/gradienterp_cloud/web/art/gradientERP-lockup.svg

The viewBox is tight-cropped to the ink, so changing baseline/x0 can change the
aspect ratio — the script prints it; if it changed, update `.wm-big{aspect-ratio}` in
prod/gradienterp_cloud/web/index.html to match (else `background:contain` letterboxes).

Requires: fontTools (`.venv`), and SF Pro at /System/Library/Fonts/SFNS.ttf (macOS).
Wordmark is SF Pro **weight 700** (matches the approved gradientERP-logo.svg) — do not
ship a lighter weight; it reads visibly slimmer. Verify against gradientERP-logo.svg
rendered in Chrome (real SF Pro) before deploying.
"""
import sys, pathlib
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.boundsPen import BoundsPen
from fontTools.varLib.instancer import instantiateVariableFont

BASE = float(sys.argv[1]) if len(sys.argv) > 1 else 167.0
X0   = float(sys.argv[2]) if len(sys.argv) > 2 else 126.0
REPO = pathlib.Path(__file__).resolve().parents[3]  # docs/images/art -> repo root

sf = TTFont("/System/Library/Fonts/SFNS.ttf")
instantiateVariableFont(sf, {"wght": 700, "opsz": 96, "wdth": 100, "GRAD": 400}, inplace=True)
upem = sf["head"].unitsPerEm
gs = sf.getGlyphSet(); cmap = sf.getBestCmap()
FS, LS = 96, -1.4
scale = FS / upem
M = lambda x: (scale, 0, 0, -scale, x, BASE)  # font units (y-up) -> SVG (y-down) at baseline

x = X0; grad = []; erp = []; bp = BoundsPen(gs)
for i, ch in enumerate("gradientERP"):
    g = cmap[ord(ch)]
    sp = SVGPathPen(gs); gs[g].draw(TransformPen(sp, M(x)))
    (grad if i < 8 else erp).append(sp.getCommands())  # "gradient" | "ERP" — one merged path each so gErp spans the word
    gs[g].draw(TransformPen(bp, M(x)))
    x += gs[g].width * scale + LS

wx0, wy0, wx1, wy1 = bp.bounds
# ∇ outer-triangle bbox under its transform (native x[126,1157] y[0,1152])
nx0, nx1 = 126 * 0.12076 + 37.785, 1157 * 0.12076 + 37.785
ny0, ny1 = 74.5, 1152 * 0.12066 + 74.5
minx, miny = min(wx0, nx0), min(wy0, ny0)
maxx, maxy = max(wx1, nx1), max(wy1, ny1)
pad = 1.5
vx, vy, vw, vh = minx - pad, miny - pad, (maxx - minx) + 2 * pad, (maxy - miny) + 2 * pad

nabla = ('<g transform="translate(37.785,74.500) scale(0.12076,0.12066)">'
         '<path fill="url(#gNabla)" d="M616 1152 126 0H1157ZM665 860 1038 77H331Z"/></g>')
svg = (
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{vw:.2f}" height="{vh:.2f}" '
    f'viewBox="{vx:.2f} {vy:.2f} {vw:.2f} {vh:.2f}" fill="none">\n  <defs>\n'
    '    <linearGradient id="gNabla" x1="0" y1="0" x2="0.45" y2="1">'
    '<stop offset="0" stop-color="#5b8cff"/><stop offset="1" stop-color="#9d6bff"/></linearGradient>\n'
    '    <linearGradient id="gErp" x1="0" y1="0.5" x2="1" y2="0.5">'
    '<stop offset="0" stop-color="#5b8cff"/><stop offset="1" stop-color="#9d6bff"/></linearGradient>\n'
    f'  </defs>\n  {nabla}\n'
    f'  <path fill="#e7e9ee" d="{" ".join(grad)}"/>\n'
    f'  <path fill="url(#gErp)" d="{" ".join(erp)}"/>\n</svg>\n'
)
for rel in ("docs/images/art/gradientERP-lockup.svg",
            "prod/gradienterp_cloud/web/art/gradientERP-lockup.svg"):
    (REPO / rel).write_text(svg)
print(f"baseline={BASE} x0={X0}  viewBox={vx:.1f} {vy:.1f} {vw:.1f} {vh:.1f}  "
      f"aspect={vw/vh:.3f}  -> set .wm-big aspect-ratio:{round(vw)}/{round(vh)}")
