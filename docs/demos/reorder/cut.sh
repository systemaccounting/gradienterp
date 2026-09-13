#!/usr/bin/env bash
# demo 05 — purchasing · CPO: the reorder loop. A barista reports a count; the agent books the
# variance, reads par off the item's rules, orders across the firm boundary, and the PO answers
# with the vendor's own delivery schedule. Rebuild:
#
#   bash docs/demos/reorder/cut.sh
#
# The segments ARE this file. Both cuts stop at 68.6 —
# the composer relights at ~68.7. Each turn's pause sits on the DARK composer; the field lighting
# up is the cut into the next turn (the demo-04 convention).
# beats, in order (both cuts share every boundary): still · typing the count report · sits ·
# send · dots/⚙ · the count read · ⚙ posting · the offer (par + vendor) · ~2s DARK pause ·
# [cut: field lights] · "yeah, Blue Ridge Roasters" · ⚙ · PO sent · dark pause · [cut] ·
# "did they take it?" · ⚙ · the payoff (accepted + shipped + ETA) · dark still · (HOLD reads it)
set -euo pipefail
cd "$(dirname "$0")"

SRC=demo-reorder.webm
CUT=../cut.sh

# ── card — three turns at speed, the bubbles legible, the churn as texture ──
HOLD=3 bash "$CUT" "$SRC" demo-reorder.gif \
  2.9-3.9@1 \
  3.9-7.1@2 \
  7.1-7.5@0.4 \
  7.5-8.3@1 \
  8.3-15.8@12 \
  15.8-17.6@1.2 \
  17.6-21.3@12 \
  21.3-22.7@1.2 \
  22.7-23.1@0.2 \
  26.0-28.6@3 \
  28.6-29.0@1 \
  29.0-36.7@12 \
  36.7-37.4@1 \
  37.4-37.8@0.4 \
  55.3-59.1@3 \
  59.1-59.5@1 \
  59.5-66.8@12 \
  66.8-68.0@1 \
  68.0-68.6@0.4

# ── full — every bubble at 1×, the ⚙ churn legible as steps ──
HOLD=4 bash "$CUT" "$SRC" demo-reorder-full.gif \
  2.9-3.9@1 \
  3.9-7.1@1.5 \
  7.1-7.5@0.3 \
  7.5-8.3@1 \
  8.3-15.8@8 \
  15.8-17.6@1 \
  17.6-21.3@8 \
  21.3-22.7@1 \
  22.7-23.1@0.2 \
  26.0-28.6@1.5 \
  28.6-29.0@1 \
  29.0-36.7@8 \
  36.7-37.4@1 \
  37.4-37.8@0.4 \
  55.3-59.1@1.5 \
  59.1-59.5@0.5 \
  59.5-66.8@8 \
  66.8-68.0@1 \
  68.0-68.6@0.4

# ── stills for agents (llms.txt § demos) — regenerated with every re-cut ──
# The end frame of the lightbox cut is the whole conversation on screen: the readable transcript.
A=../../../prod/gradienterp_cloud/assets
N=$(ffprobe -v error -count_frames -select_streams v -show_entries stream=nb_read_frames -of csv=p=0 demo-reorder-full.gif)
ffmpeg -y -v error -i demo-reorder-full.gif -vf "select='eq(n\,$((N-1)))'" -frames:v 1 "$A/demo-reorder-end.png"
# it scrolls — the mid frame holds the first exchange the end frame scrolls past: the webm at the
# post-offer dark pause (see the beat comments above)
ffmpeg -y -v error -ss 23.0 -i demo-reorder.webm -frames:v 1 "$A/demo-reorder-mid.png"

# row thumbnail — the card gif at thumb scale (the login rows animate without pulling full gifs;
# the tap pulls demo-reorder-full.gif in the lightbox)
ffmpeg -y -v error -i demo-reorder.gif -vf "fps=10,scale=-2:128:flags=lanczos,split[a][b];[a]palettegen=max_colors=64[p];[b][p]paletteuse=dither=bayer" "$A/demo-reorder-thumb.gif"
# lightbox video — the full cut as mp4 (~10x lighter than the gif; the lightbox plays this,
# falling back to the gif only if video fails)
ffmpeg -y -v error -i demo-reorder-full.gif -movflags +faststart -pix_fmt yuv420p -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" -crf 27 -an "$A/demo-reorder-full.mp4"
