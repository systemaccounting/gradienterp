#!/usr/bin/env bash
# demo 02 — CMO, one prompt: who are our top 5 customers. Rebuild:
#
#   bash docs/demos/contacts/cut.sh
#
# The segments ARE this file. One prompt, so ONE gif —
# the card and the lightbox are the same file for a single-movement demo.
# beats, in order: still · typing · the finished question sits · send · dots · ⚙ reading your
# invoices · the top-5 list streams · (HOLD reads it)
set -euo pipefail
cd "$(dirname "$0")"

# Ends at 17.6: the composer border lights up at 17.75, which reads as the demo waiting on you
# rather than having finished. The reading beat is HOLD, not a longer span.
HOLD=4 bash ../cut.sh demo-contacts.webm demo-contacts.gif \
  1.3-2.3@1 \
  2.3-7.2@1.5 \
  7.2-7.75@0.5 \
  7.75-8.6@1 \
  8.6-13.4@8 \
  13.4-16.2@2 \
  16.2-17.6@1

# ── stills for agents (llms.txt § demos) — regenerated with every re-cut ──
# The end frame of the lightbox cut is the whole conversation on screen: the readable transcript.
A=../../../prod/gradienterp_cloud/assets
N=$(ffprobe -v error -count_frames -select_streams v -show_entries stream=nb_read_frames -of csv=p=0 demo-contacts.gif)
ffmpeg -y -v error -i demo-contacts.gif -vf "select='eq(n\,$((N-1)))'" -frames:v 1 "$A/demo-contacts-end.png"

# row thumbnail — the card gif at thumb scale (the login rows animate without pulling full gifs;
# the tap pulls demo-contacts-full.gif in the lightbox)
ffmpeg -y -v error -i demo-contacts.gif -vf "fps=10,scale=-2:128:flags=lanczos,split[a][b];[a]palettegen=max_colors=64[p];[b][p]paletteuse=dither=bayer" "$A/demo-contacts-thumb.gif"
# lightbox video — the full cut as mp4 (~10x lighter than the gif; the lightbox plays this,
# falling back to the gif only if video fails)
ffmpeg -y -v error -i demo-contacts.gif -movflags +faststart -pix_fmt yuv420p -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" -crf 27 -an "$A/demo-contacts-full.mp4"
