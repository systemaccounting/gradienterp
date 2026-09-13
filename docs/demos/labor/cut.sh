#!/usr/bin/env bash
# demo 03 — COO, one vague prompt: "schedule the FOH for the next 2 weeks". Rebuild:
#
#   bash docs/demos/labor/cut.sh
#
# The segments ARE this file. Both cuts stop at 33.6 —
# the composer lights up at 33.7, and ending on a lit input reads as the demo waiting on you rather
# than having finished.
# beats, in order: still · typing · prompt sits · send · dots/⚙ · the lineup streams (one line
# per person) · complete, dark · (HOLD reads it)
set -euo pipefail
cd "$(dirname "$0")"

SRC=demo-labor.webm
CUT=../cut.sh

# ── card — prompt → work → lineup ──
# The whole take is 39s, so there is very little to compress away. The one long span is 13.3→30.3:
# a single narration line and three ⚙ working rows, seventeen seconds in which the screen barely
# changes. That is the only stretch spending a viewer's attention on nothing, so it takes the
# heaviest speedup and everything else stays close to real time.
HOLD=3 bash "$CUT" "$SRC" demo-labor.gif \
  0.8-2.2@1 \
  2.2-5.5@1.5 \
  5.5-6.2@0.4 \
  6.2-10.3@8 \
  10.3-13.3@4 \
  13.3-30.3@8 \
  30.3-32.7@1.5 \
  32.7-33.6@1

# ── full — the same, with the lineup arriving at 1× ──
# The answer is eight lines now rather than thirty, so the card already carries it. This cut is for
# the lightbox: the schedule lands slowly enough to read every row, and the working span gets a
# little more room so the audit trail reads as steps rather than a blur.
HOLD=4 bash "$CUT" "$SRC" demo-labor-full.gif \
  0.8-2.2@1 \
  2.2-5.5@1.5 \
  5.5-6.2@0.4 \
  6.2-10.3@8 \
  10.3-13.3@3 \
  13.3-30.3@6 \
  30.3-32.7@1 \
  32.7-33.6@1

# ── stills for agents (llms.txt § demos) — regenerated with every re-cut ──
# The end frame of the lightbox cut is the whole conversation on screen: the readable transcript.
A=../../../prod/gradienterp_cloud/assets
N=$(ffprobe -v error -count_frames -select_streams v -show_entries stream=nb_read_frames -of csv=p=0 demo-labor-full.gif)
ffmpeg -y -v error -i demo-labor-full.gif -vf "select='eq(n\,$((N-1)))'" -frames:v 1 "$A/demo-labor-end.png"

# row thumbnail — the card gif at thumb scale (the login rows animate without pulling full gifs;
# the tap pulls demo-labor-full.gif in the lightbox)
ffmpeg -y -v error -i demo-labor.gif -vf "fps=10,scale=-2:128:flags=lanczos,split[a][b];[a]palettegen=max_colors=64[p];[b][p]paletteuse=dither=bayer" "$A/demo-labor-thumb.gif"
# lightbox video — the full cut as mp4 (~10x lighter than the gif; the lightbox plays this,
# falling back to the gif only if video fails)
ffmpeg -y -v error -i demo-labor-full.gif -movflags +faststart -pix_fmt yuv420p -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" -crf 27 -an "$A/demo-labor-full.mp4"
