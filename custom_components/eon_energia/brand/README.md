# Brand assets

Home Assistant serves these. Since **2026.3** a custom integration ships its own brand
images in a `brand/` folder inside the integration, and Home Assistant exposes them at
`/api/brands/integration/eon_energia/`, taking priority over the brands CDN. The central
[home-assistant/brands](https://github.com/home-assistant/brands) repository no longer
accepts custom integrations, so there is nothing to submit anywhere — the files here are
the whole story.

See the [Brands Proxy API announcement](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/).

On Home Assistant older than 2026.3 the integration shows the generic puzzle-piece icon.
Nothing breaks.

## What is here

| File | Size | Used for |
| --- | --- | --- |
| `icon.png` | 256×256 | The integration tile, the device page, the "add integration" list |
| `icon@2x.png` | 512×512 | The same, on a retina display |
| `logo.png` | 256×79 | The wordmark, shown in the config flow header |
| `logo@2x.png` | 512×158 | The same, on a retina display |

No `dark_` variants: the icon is a self-contained tile with its own background, so it
reads on either theme, and the logo is E.ON red on transparency, which has enough
contrast both ways.

## The artwork

The wordmark is **E.ON's own**, taken verbatim as vector from
`myeon.eon-energia.com/content/dam/eon-scsi/new-ciam/logoEon.svg`. It is not traced or
redrawn; only its fill and placement change. The tile gradient is likewise measured
rather than invented, sampled off the E.ON Android app's own launcher foreground
(`res/mipmap-xxxhdpi/ic_launcher_foreground.png`) with the white wordmark masked out. It
runs along the top-left → bottom-right diagonal and is very nearly symmetric about the
other one: magenta at both of those corners, with a red plateau through the middle.

It is worth noting that averaging colour per radius band makes that gradient look radial.
It is not — points at equal radius differ by direction, and the anti-diagonal is red from
end to end.

The tile itself is a **squircle** — a rounded rectangle with continuous curvature, the
shape `UIBezierPath(roundedRect:cornerRadius:)` produces and Figma exposes as "corner
smoothing". Not a superellipse: that curves continuously everywhere, so the edges that
should be flat bow outward and the tile reads as pillowed next to a real app icon.

The wordmark is optically centred, not box-centred. The generator renders it, weighs the
opaque pixels, and corrects 70% of the way toward the ink centroid — correcting the whole
way crowds the silhouette against an edge, because a shape's extent counts perceptually
as well as its weight. It then lifts the artwork 1.5%, because the perceived centre of a
frame sits slightly above its geometric centre.

## Regenerating

`icon.svg` and `logo.svg` are generated, not hand-edited. Change `tools/make_icon.py` and
re-run it — it writes both sources and renders every PNG:

```sh
python tools/make_icon.py
```

To render an SVG you drew elsewhere, skip the generator and use:

```sh
python tools/render_brand.py custom_components/eon_energia/brand/icon.svg
```

Both need `rsvg-convert` (`brew install librsvg`). Use it rather than Chromium or
Inkscape — librsvg is what Home Assistant's own tooling uses, and the three disagree
about filters. The optical-centring step also wants Pillow; without it the generator
falls back to box centring and says so.

## Two things that will bite you

**`feComponentTransfer` renders with a visible rectangular seam in librsvg**, which is
what Home Assistant's tooling and most Linux boxes use. If you need a glow, stack two
blurred passes instead — it looks the same and renders consistently.

**Blur radii live in the coordinate space of whatever transform encloses them.** Writing
the pixel radius you want directly, inside a group that is scaled up, gives a blur that
overruns its filter region and leaves a hard rectangular clip edge.
