#!/usr/bin/env bash
# Cut a demo webm to a gif as a list of variably-sped segments.
#
#   bash docs/demos/cut.sh <in.webm> <out.gif> <seg> [seg ...]
#   seg = START-END@SPEED   (source seconds, SPEED > 1 is faster)
#
#   bash docs/demos/cut.sh out/demo-contacts.webm out/demo-contacts.gif \
#     4.0-6.5@7 6.5-9.0@1 9.0-21.0@8 21.0-23.2@1
#
# Why segments: a single trim at a single speed is what makes demo gifs unreadable. A take mixes
# beats that need opposite treatment — a bubble the viewer must READ wants 1×, the typing and the ⚙
# working beats want ~6-8×, and the dead model-thinking pause wants to be gone. Pacing doctrine and
# how to pick the numbers: docs/demos/AGENTS.md § webm → gif. There is no duration target.
#
# One palette is generated across the CONCATENATED result rather than per segment — per-segment
# palettes make the colors shift at every cut.
set -euo pipefail

W=${W:-880}       # in-camera framing width (no crop)
FPS=${FPS:-15}
HOLD=${HOLD:-0}   # seconds to hang on the LAST frame before the loop restarts (see below)

[ $# -ge 3 ] || { sed -n '2,12p' "$0"; exit 1; }
IN=$1; OUT=$2; shift 2
[ -f "$IN" ] || { echo "no such webm: $IN" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

i=0
: > "$TMP/list.txt"
for seg in "$@"; do
  [[ $seg =~ ^([0-9.]+)-([0-9.]+)@([0-9.]+)$ ]] || { echo "bad segment '$seg' (want START-END@SPEED)" >&2; exit 1; }
  t0=${BASH_REMATCH[1]}; t1=${BASH_REMATCH[2]}; spd=${BASH_REMATCH[3]}
  part=$(printf "%s/p%02d.mp4" "$TMP" "$i")
  # -ss before -i seeks fast; re-encode so setpts applies and the parts concat cleanly
  ffmpeg -v error -y -ss "$t0" -to "$t1" -i "$IN" \
    -vf "setpts=PTS/$spd,fps=$FPS,scale=$W:-1:flags=lanczos" -an "$part"
  echo "file '$part'" >> "$TMP/list.txt"
  printf "  %ss→%ss @%sx\n" "$t0" "$t1" "$spd"
  i=$((i + 1))
done

ffmpeg -v error -y -f concat -safe 0 -i "$TMP/list.txt" -c copy "$TMP/joined.mp4"
ffmpeg -v error -y -i "$TMP/joined.mp4" -vf "palettegen=stats_mode=diff" "$TMP/pal.png"

# HOLD hangs on the LAST frame. GIF stores a delay per frame, so this costs one frame and any
# number of seconds — the cheapest thing in the file, and the right way to give a viewer time to
# read the final answer before the loop restarts. Buy end-of-gif time here, never by slowing the
# last segment: a slowed still frame is just duplicate frames, which is bytes for nothing.
#
# NO mpdecimate here, deliberately. It looks like the obvious win (it halved these files) but it
# DELETES duration rather than redistributing it — measured: an 11.7s cut with a 0.5s span slowed
# to 1.5s came out 1.0s short, exactly the duplicate time the slowdown added. `-vsync 0` and
# `-fps_mode passthrough` don't save it. So it silently removes the beats a cut is built out of,
# which is a bad trade for ~20% of the bytes.
DELAY_ARG=()
if [ "$HOLD" != "0" ]; then
  DELAY_ARG=(-final_delay "$(echo "$HOLD * 100" | bc)")
fi
ffmpeg -v error -y -i "$TMP/joined.mp4" -i "$TMP/pal.png" \
  -lavfi "paletteuse=dither=bayer" "${DELAY_ARG[@]}" -loop 0 "$OUT"

dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT")
printf "%s — %.1fs, %s\n" "$OUT" "$dur" "$(du -h "$OUT" | cut -f1)"
