#!/usr/bin/env bash
# demo 01 — CFO, two prompts (statements → the analyst room). Re-cut both artifacts:
#
#   bash docs/demos/accounting/cut.sh
#
# Everything for this demo lives in this directory: the source take, the segments, the reasoning,
# and the cut gifs. The segments ARE this file. What the demo is
# and why is README.md. The cutting itself is docs/demos/cut.sh, which is general and never edited
# per demo.
set -euo pipefail
cd "$(dirname "$0")"                       # this demo's directory — source in, gifs out

SRC=demo-analysis.webm                     # ONE take carrying both prompts
CUT=../cut.sh

# ── card (login page, autoplays next to its siblings) — prompt 1 only ──
# Ends at 69.9: at 70.0 the composer lights up asking for the next question, which reads as the demo
# waiting on you rather than having finished. The end beat is HOLD (a per-frame GIF delay), never a
# slowed last segment — slowing a still frame is just duplicate frames.
HOLD=3 bash "$CUT" "$SRC" demo-accounting.gif \
  3.5-6.5@1.5 \
  6.5-7.0@0.33 \
  7.0-8.5@1 \
  8.5-11.5@8 \
  11.5-19.8@3 \
  20-68.5@24 \
  68.5-69.9@1

# ── full (opens from the card) — both prompts ──
# Here the composer activating IS the lead-in to prompt 2, so this one runs through it.
# Both typing segments are @1.5 — one typing RATE across the set, whatever the prompt's length.
HOLD=2 bash "$CUT" "$SRC" demo-accounting-full.gif \
  3.5-6.5@1.5 \
  6.5-7.0@0.33 \
  7.0-8.5@1 \
  8.5-11.5@8 \
  11.5-19.8@3 \
  20-68.5@20 \
  68.5-71.5@1 \
  73.5-80.5@1.5 \
  80.5-83.5@1 \
  83.5-121.5@14 \
  121.5-127@2 \
  127-134@1

# ── stills for agents (llms.txt § demos) — regenerated with every re-cut ──
# The end frame of the lightbox cut is the whole conversation on screen: the readable transcript.
A=../../../prod/gradienterp_cloud/assets
N=$(ffprobe -v error -count_frames -select_streams v -show_entries stream=nb_read_frames -of csv=p=0 demo-accounting-full.gif)
ffmpeg -y -v error -i demo-accounting-full.gif -vf "select='eq(n\,$((N-1)))'" -frames:v 1 "$A/demo-accounting-end.png"
# the demo scrolls and spans TWO prompts — the card's last frame is prompt 1's finished answer
# (the statements block), which is the middle the end frame scrolls past
M=$(ffprobe -v error -count_frames -select_streams v -show_entries stream=nb_read_frames -of csv=p=0 demo-accounting.gif)
ffmpeg -y -v error -i demo-accounting.gif -vf "select='eq(n\,$((M-1)))'" -frames:v 1 "$A/demo-accounting-mid.png"

# row thumbnail — the card gif at thumb scale (the login rows animate without pulling full gifs;
# the tap pulls demo-accounting-full.gif in the lightbox)
ffmpeg -y -v error -i demo-accounting.gif -vf "fps=10,scale=-2:128:flags=lanczos,split[a][b];[a]palettegen=max_colors=64[p];[b][p]paletteuse=dither=bayer" "$A/demo-accounting-thumb.gif"
# lightbox video — the full cut as mp4 (~10x lighter than the gif; the lightbox plays this,
# falling back to the gif only if video fails)
ffmpeg -y -v error -i demo-accounting-full.gif -movflags +faststart -pix_fmt yuv420p -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" -crf 27 -an "$A/demo-accounting-full.mp4"
