# brand art — open work

## the lockup

`gradientERP-lockup.svg` is the **fully-outlined wordmark lockup** (∇ path + every glyph as a
`<path>`, no `<text>`/`font-family`) — font-independent, so it renders identically on every
device. It's the only shipped asset: wired into the web login/auth banner
(`prod/gradienterp_cloud/web/`, served at `/art/`, sized via the `.wm-big` class).

**It is generated — don't hand-edit it.** `build-lockup.py` is the source of truth:

```
.venv/bin/python docs/images/art/build-lockup.py [baseline] [x0]
```

- wordmark is **SF Pro weight 700** (matches the approved `gradientERP-logo.svg`); a lighter
  weight reads visibly slimmer. The ∇ reuses the extracted path from `gradientERP-nabla.svg`
  (`Apple Symbols` ∇ — Georgia has no U+2207, so a glyph would substitute).
- `baseline` (lower → wordmark higher in the ∇) and `x0` (lower → wordmark left, more into the
  ∇) are the two nudge knobs; current shipped values are the script defaults.
- the viewBox is tight-cropped, so a nudge can change the aspect — the script prints the
  `aspect-ratio` to set on `.wm-big` in `web/index.html`.
- need a raster (social, README)? export on demand: `rsvg-convert -w 1600 gradientERP-lockup.svg -o out.png`.

Verify against `gradientERP-logo.svg` (the approved SF-Pro-700 reference) rendered in Chrome
before deploying.

## open

- the **appbar** lockup (home/gerp screens) is still small CSS-text `∇`+wordmark — substitution
  barely shows at that size; swap to the outlined SVG for consistency if desired.
