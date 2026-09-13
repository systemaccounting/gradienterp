#!/usr/bin/env bash
# demo 04 — the INVESTOR's side: Westwood bids to buy a rule off Tanners' published margin, and
# Tanners takes it. Rebuild:
#
#   bash docs/demos/invest/cut.sh
#
# The segments ARE this file. Both cuts stop at 59.3 —
# the composer relights at ~59.4, and ending on a lit input reads as the demo waiting on you rather
# than having finished.
# beats, in order (both cuts share every boundary): still · typing the bid · prompt sits · send ·
# dots · ⚙ · the bid lands (terms + inbox closer) · ~2s DARK pause · [cut: field lights] ·
# typing "did they take it?" · send · ⚙ · the payoff (deal settled + outlay booked at accept) ·
# dark still · (HOLD reads it)
set -euo pipefail
cd "$(dirname "$0")"

SRC=demo-invest.webm
CUT=../cut.sh

# ── card — bid → they take it ──
# Two turns means two of everything, so this card runs long for the set. The typing still gets close
# to real time — a prompt that teleports in reads as a machine filling a form, and the whole point is
# a person deciding to place a bid. The compression comes out of the two dead spans instead.
HOLD=3 bash "$CUT" "$SRC" demo-invest.gif \
  2.2-3.4@1 \
  3.4-17.7@2 \
  17.7-17.9@0.2 \
  17.9-18.3@1 \
  18.3-22.9@12 \
  22.9-31.8@10 \
  31.8-34.4@1.2 \
  34.4-35.3@0.45 \
  47.0-49.3@3 \
  49.3-50.0@1 \
  50.0-53.8@10 \
  53.8-58.6@1.2 \
  58.6-59.3@1

# ── full — the same, both answers at 1× ──
# The first answer is four terms lines the viewer should actually read (price, rate, cap, and what
# it books as); the second is the payoff. The lightbox gives both real time and lets the ⚙ churn
# stay legible as steps rather than a blur.
HOLD=4 bash "$CUT" "$SRC" demo-invest-full.gif \
  2.2-3.4@1 \
  3.4-17.7@1.5 \
  17.7-17.9@0.15 \
  17.9-18.3@1 \
  18.3-22.9@8 \
  22.9-31.8@4 \
  31.8-34.4@1 \
  34.4-35.3@0.45 \
  47.0-47.3@1 \
  47.3-49.3@1.5 \
  49.3-50.0@0.5 \
  50.0-53.8@5 \
  53.8-58.6@1 \
  58.6-59.3@1

# ── stills for agents (llms.txt § demos) — regenerated with every re-cut ──
# The end frame of the lightbox cut is the whole conversation on screen: the readable transcript.
A=../../../prod/gradienterp_cloud/assets
N=$(ffprobe -v error -count_frames -select_streams v -show_entries stream=nb_read_frames -of csv=p=0 demo-invest-full.gif)
ffmpeg -y -v error -i demo-invest-full.gif -vf "select='eq(n\,$((N-1)))'" -frames:v 1 "$A/demo-invest-end.png"
# it scrolls — the mid frame holds the first exchange the end frame scrolls past: the webm at the
# turn-1 dark pause (bid landed, composer still dark; see the beat comments above)
ffmpeg -y -v error -ss 35.0 -i demo-invest.webm -frames:v 1 "$A/demo-invest-mid.png"

# row thumbnail — the card gif at thumb scale (the login rows animate without pulling full gifs;
# the tap pulls demo-invest-full.gif in the lightbox)
ffmpeg -y -v error -i demo-invest.gif -vf "fps=10,scale=-2:128:flags=lanczos,split[a][b];[a]palettegen=max_colors=64[p];[b][p]paletteuse=dither=bayer" "$A/demo-invest-thumb.gif"
# lightbox video — the full cut as mp4 (~10x lighter than the gif; the lightbox plays this,
# falling back to the gif only if video fails)
ffmpeg -y -v error -i demo-invest-full.gif -movflags +faststart -pix_fmt yuv420p -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" -crf 27 -an "$A/demo-invest-full.mp4"
