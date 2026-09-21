#!/usr/bin/env python3
"""Generate the E.ON Energia brand icon.

    python tools/make_icon.py

Writes `icon.svg` and renders every PNG size from it. The SVG is build output — change
this script and re-run it, never edit the SVG by hand, or the next run silently reverts
your edit.

The artwork is E.ON's own wordmark, taken verbatim as vector from
`myeon.eon-energia.com/content/dam/eon-scsi/new-ciam/logoEon.svg`, in white on a
full-bleed squircle in E.ON red. The wordmark path is not redrawn or traced; only its
fill and placement change.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The artboard. Home Assistant only ever serves the 256 and 512 renders, but the source
#: is drawn at 1024 so the maths below is in round numbers.
SIZE = 1024

#: Apple's app-icon corner, as a fraction of the width, with Figma's equivalent
#: corner-smoothing value. These two numbers *are* the iOS icon shape; see squircle().
CORNER_RADIUS_RATIO = 0.2237
CORNER_SMOOTHING = 0.6

#: E.ON red, as published in their own logo SVG.
BRAND_RED = "#EA1B0A"

#: The tile gradient, measured off E.ON's own app icon rather than invented.
#:
#: It runs along the top-left -> bottom-right diagonal and is very nearly symmetric
#: about the other one: magenta at both of those corners, with a red plateau through
#: the middle. (Averaging colour per radius band makes it look radial. It is not -
#: points at equal radius differ by direction, and the anti-diagonal is red end to end.)
#:
#: Sampled from res/mipmap-xxxhdpi/ic_launcher_foreground.png with the white wordmark
#: masked out, then rescaled so the disc's visible span maps onto the full tile.
GRADIENT_STOPS = (
    (0.00, "#93147F"),
    (0.15, "#B7105D"),
    (0.28, "#D01438"),
    (0.40, "#E31C1A"),
    (0.50, "#EA1D0B"),
    (0.62, "#EA1C0B"),
    (0.74, "#D2192A"),
    (0.86, "#B31654"),
    (1.00, "#981479"),
)

#: The wordmark, verbatim from E.ON's logoEon.svg (viewBox 0 0 120 37).
WORDMARK_VIEWBOX = (120.0, 37.0)
WORDMARK_PATH = (
    "M120 13.9305C120 17.9505 115.215 31.7205 114.431 33.7867C113.914 35.1442 112.927 "
    "35.3355 111.645 35.3355C109.327 35.3355 107.58 34.7842 106.729 34.1017C106.552 "
    "33.963 106.335 33.6592 106.481 33.2167C107.351 30.6217 110.659 21.273 110.659 "
    "19.2517C110.659 18.1455 110.333 17.313 109.23 17.313C102.536 17.313 90.2363 "
    "31.5967 87.9525 34.1505C87.2063 34.983 86.1113 35.1592 84.8325 35.1592C83.25 "
    "35.1592 81.5663 34.3192 81.405 34.2367C80.9062 33.978 80.88 33.693 81.03 "
    "33.0892L81.45 31.3792C82.2188 28.2517 86.2013 16.683 88.965 10.413C89.0813 "
    "10.1467 89.1975 9.92922 89.7975 9.83547C90.075 9.79047 90.99 9.57672 92.2613 "
    "9.57672C93.195 9.57672 95.01 9.64422 95.9175 10.1317C95.9175 10.1317 95.925 "
    "10.1167 95.925 11.643C95.925 12.303 96 14.5455 98.1075 14.5455C101.666 14.5455 "
    "108.131 8.43672 113.842 8.43672C118.868 8.43672 120 11.5342 120 13.9305ZM48.9225 "
    "18.9855C48.9225 19.818 47.9512 22.1167 47.8125 22.458C46.9388 24.6142 44.6475 "
    "24.963 43.0312 24.963C40.6125 24.963 39.78 24.093 39.78 22.7205C39.78 21.6592 "
    "40.6725 19.8255 40.7887 19.5555C41.88 17.058 43.83 16.788 45.7575 16.788C47.3887 "
    "16.788 48.9225 17.4667 48.9225 18.9855ZM28.2188 13.7092C28.2188 12.1192 28.1437 "
    "9.84297 26.8988 9.84297C22.8525 9.84297 15.7462 16.0305 15.0337 16.743C14.505 "
    "17.2717 14.8987 17.2267 15.6037 17.2267H24.7013C27.9263 17.2267 28.2188 16.683 "
    "28.2188 13.7092ZM37.935 14.5455C37.935 22.3342 33.1425 23.9505 27.825 "
    "23.9505H24.0863C20.355 23.9505 12.8363 23.733 12.8363 23.733C12.2325 23.718 "
    "11.7788 23.598 11.7788 23.9505C11.7788 26.493 14.655 30.3255 19.0312 "
    "30.3255C22.4812 30.3255 24.7013 28.173 25.185 27.663C25.185 27.663 29.1488 "
    "27.2055 31.6912 27.2055C34.1625 27.2055 37.8 27.5992 37.8 29.0055C37.8 31.7505 "
    "28.8863 36.0817 21.405 36.0817C10.1438 36.0817 0 29.9655 0 22.5892C0 10.2292 "
    "23.8538 0.917969 30.8137 0.917969C37.08 0.917969 37.935 10.8967 37.935 "
    "14.5455ZM67.8225 14.283C66.975 14.283 65.715 14.4142 65.1413 15.2055C63 18.168 "
    "61.5825 25.5817 61.5825 27.6442C61.5825 29.0505 62.5612 28.9605 63.8662 "
    "28.9605C64.6275 28.9605 65.8538 28.983 66.5475 28.2592C68.43 26.298 70.0537 "
    "17.4217 70.0537 15.8167C70.0537 14.6767 69.9713 14.283 67.8225 14.283ZM81.975 "
    "13.8855C81.975 17.2717 78.6338 24.7005 77.5612 26.763C73.0725 35.3955 67.0763 "
    "35.3355 60.3075 35.3355C56.9963 35.3355 49.6237 35.373 49.6237 29.1817C49.6237 "
    "26.4142 52.0387 21.3367 54.0225 16.5217C54.8625 14.478 58.455 7.60047 70.68 "
    "7.60047C75.7237 7.60047 81.975 8.39172 81.975 13.8855Z"
)

#: How much of the tile width the wordmark spans. E.ON set their own app icon at roughly
#: two thirds; much wider and the squircle's corners start clipping the descenders.
WORDMARK_WIDTH_RATIO = 0.66

#: How far to correct toward the measured ink centroid. Correcting the whole way crowds
#: the silhouette against an edge, because a shape's extent counts perceptually as well
#: as its weight; see the brand-assets skill.
CENTROID_CORRECTION = 0.70

#: The perceived centre of a frame sits slightly above its geometric centre, so artwork
#: centred purely by measurement looks sunken. As a fraction of the artboard.
OPTICAL_LIFT = 0.015


def squircle(size: float) -> str:
    """Return the iOS app-icon shape as an SVG path, inscribed in a `size` square.

    Not a superellipse. The obvious implementation — `|x|^n + |y|^n = 1` with n
    around 5 — is the shape most people mean by "squircle", but it is not the one
    Apple uses, and the difference is visible: a superellipse curves continuously
    everywhere, so the edges that should be flat bow outward. Side by side against
    a real app icon it reads as pillowed.

    Apple's mask is a *rounded rectangle with continuous curvature*, as produced by
    `UIBezierPath(roundedRect:cornerRadius:)`: genuinely straight edges, with the
    corner easing curvature in over a longer run than a circular arc would. This is
    the construction Figma exposes as "corner smoothing", at the iOS values.

    A circular corner is still wrong for the reason you would expect — curvature
    jumps from 0 to 1/r at the join — but the fix is smoothing the transition, not
    abandoning the straight edge.
    """
    radius = size * CORNER_RADIUS_RATIO
    budget = size / 2

    # Keep the corner inside the space available to it; without the cap a large
    # radius yields a self-intersecting path rather than a clipped one.
    smoothing = min(CORNER_SMOOTHING, budget / radius - 1)
    p = min((1 + smoothing) * radius, budget)

    arc_measure = math.radians(90 * (1 - smoothing))
    arc = math.sin(arc_measure / 2) * radius * math.sqrt(2)

    angle_alpha = (math.pi / 2 - arc_measure) / 2
    angle_beta = math.radians(45 * smoothing)
    c = radius * math.tan(angle_alpha / 2) * math.cos(angle_beta)
    d = c * math.tan(angle_beta)

    # The straight run into each corner splits 2:1 between the two off-curve
    # control points. That ratio is what ramps curvature smoothly.
    b = (p - arc - c - d) / 3
    a = 2 * b

    def f(value: float) -> str:
        return f"{value:.4f}"

    return " ".join(
        [
            f"M {f(size - p)} 0",
            f"c {f(a)} 0 {f(a + b)} 0 {f(a + b + c)} {f(d)}",
            f"a {f(radius)} {f(radius)} 0 0 1 {f(arc)} {f(arc)}",
            f"c {f(d)} {f(c)} {f(d)} {f(b + c)} {f(d)} {f(a + b + c)}",
            f"L {f(size)} {f(size - p)}",
            f"c 0 {f(a)} 0 {f(a + b)} {f(-d)} {f(a + b + c)}",
            f"a {f(radius)} {f(radius)} 0 0 1 {f(-arc)} {f(arc)}",
            f"c {f(-c)} {f(d)} {f(-(b + c))} {f(d)} {f(-(a + b + c))} {f(d)}",
            f"L {f(p)} {f(size)}",
            f"c {f(-a)} 0 {f(-(a + b))} 0 {f(-(a + b + c))} {f(-d)}",
            f"a {f(radius)} {f(radius)} 0 0 1 {f(-arc)} {f(-arc)}",
            f"c {f(-d)} {f(-c)} {f(-d)} {f(-(b + c))} {f(-d)} {f(-(a + b + c))}",
            f"L 0 {f(p)}",
            f"c 0 {f(-a)} 0 {f(-(a + b))} {f(d)} {f(-(a + b + c))}",
            f"a {f(radius)} {f(radius)} 0 0 1 {f(arc)} {f(-arc)}",
            f"c {f(c)} {f(-d)} {f(b + c)} {f(-d)} {f(a + b + c)} {f(-d)}",
            "Z",
        ]
    )


def build(dx: float = 0.0, dy: float = 0.0) -> str:
    """Return the SVG source, with the wordmark nudged by (dx, dy) artboard units."""
    mark_w, mark_h = WORDMARK_VIEWBOX
    scale = SIZE * WORDMARK_WIDTH_RATIO / mark_w
    x = (SIZE - mark_w * scale) / 2 + dx
    y = (SIZE - mark_h * scale) / 2 + dy

    # The gradient axis is the top-left -> bottom-right diagonal, corner to corner,
    # so the tile's extremes land on the end stops the way the disc's rim does.
    stops = "\n".join(
        f'      <stop offset="{offset:g}" stop-color="{colour}"/>'
        for offset, colour in GRADIENT_STOPS
    )

    return f"""\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" \
width="{SIZE}" height="{SIZE}">
  <title>E.ON Energia</title>

  <!-- Generated by tools/make_icon.py. Do not edit; re-run the script. -->
  <!-- Wordmark is E.ON's own, taken verbatim from their logoEon.svg. -->

  <defs>
    <linearGradient id="tile" gradientUnits="userSpaceOnUse"
        x1="0" y1="0" x2="{SIZE}" y2="{SIZE}">
{stops}
    </linearGradient>
  </defs>

  <path d="{squircle(SIZE)}" fill="url(#tile)"/>

  <g transform="translate({x:.4f} {y:.4f}) scale({scale:.6f})">
    <path fill-rule="evenodd" clip-rule="evenodd" fill="#FFFFFF" d="{WORDMARK_PATH}"/>
  </g>
</svg>
"""


def build_logo() -> str:
    """Return the wordmark on its own, for the config flow header.

    Home Assistant shows `logo.png` where it wants the brand rather than the app
    tile. That is the wordmark by itself, in E.ON red, on transparency - not the
    square icon again, which is what this repository used to ship.
    """
    mark_w, mark_h = WORDMARK_VIEWBOX
    return f"""\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {mark_w:g} {mark_h:g}" \
width="{mark_w:g}" height="{mark_h:g}">
  <title>E.ON</title>

  <!-- Generated by tools/make_icon.py. Do not edit; re-run the script. -->
  <!-- Wordmark is E.ON's own, taken verbatim from their logoEon.svg. -->

  <path fill-rule="evenodd" clip-rule="evenodd" fill="{BRAND_RED}" d="{WORDMARK_PATH}"/>
</svg>
"""


def _ink_offset() -> tuple[float, float]:
    """Measure how far the wordmark's ink sits from the centre of the artboard.

    Bounding-box centring is not optical centring: a shape whose mass is off to one
    side still looks off-centre when its box is centred. Render the wordmark alone,
    weigh the opaque pixels, and report the centroid's offset in artboard units.

    Returns (0, 0) if Pillow is unavailable, which only costs a little precision.
    """
    try:
        from PIL import Image
    except ImportError:
        print("Pillow not installed - skipping optical centring", file=sys.stderr)
        return 0.0, 0.0

    mark_w, mark_h = WORDMARK_VIEWBOX
    scale = SIZE * WORDMARK_WIDTH_RATIO / mark_w
    probe = ROOT / "tools" / ".probe.svg"
    probe_png = ROOT / "tools" / ".probe.png"
    probe.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" '
        f'width="{SIZE}" height="{SIZE}">'
        f'<g transform="translate({(SIZE - mark_w * scale) / 2:.4f} '
        f'{(SIZE - mark_h * scale) / 2:.4f}) scale({scale:.6f})">'
        f'<path fill-rule="evenodd" clip-rule="evenodd" fill="#000000" '
        f'd="{WORDMARK_PATH}"/></g></svg>'
    )
    try:
        subprocess.run(
            ["rsvg-convert", "--width", str(SIZE), "--height", str(SIZE),
             "--background-color", "none", "--output", str(probe_png), str(probe)],
            check=True,
        )
        alpha = Image.open(probe_png).convert("RGBA").split()[-1]
        total = sx = sy = 0
        for y, row in enumerate(
            [alpha.crop((0, y, SIZE, y + 1)).tobytes() for y in range(SIZE)]
        ):
            for x, a in enumerate(row):
                if a:
                    total += a
                    sx += a * x
                    sy += a * y
        if not total:
            return 0.0, 0.0
        return SIZE / 2 - sx / total, SIZE / 2 - sy / total
    finally:
        probe.unlink(missing_ok=True)
        probe_png.unlink(missing_ok=True)


def main() -> None:
    """Write the SVG and render the PNGs beside it."""
    if not shutil.which("rsvg-convert"):
        raise SystemExit("rsvg-convert is not installed - brew install librsvg")

    off_x, off_y = _ink_offset()
    dx = off_x * CENTROID_CORRECTION
    dy = off_y * CENTROID_CORRECTION - SIZE * OPTICAL_LIFT
    print(f"ink centroid offset ({off_x:+.1f}, {off_y:+.1f}) -> nudge ({dx:+.1f}, {dy:+.1f})")

    source = ROOT / "custom_components" / "eon_energia" / "brand" / "icon.svg"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(build(dx, dy))
    print(source)

    logo = source.with_name("logo.svg")
    logo.write_text(build_logo())
    print(logo)

    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "render_brand.py"), str(source), str(logo)],
        check=True,
    )


if __name__ == "__main__":
    main()
