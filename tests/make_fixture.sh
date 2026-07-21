#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/tests/fixtures"
ffmpeg -v error -y \
  -f lavfi -i "color=c=red:s=640x360:r=24:d=1.5" \
  -f lavfi -i "color=c=blue:s=640x360:r=24:d=1.2" \
  -f lavfi -i "color=c=green:s=640x360:r=24:d=1.8" \
  -filter_complex "[0:v][1:v][2:v]concat=n=3:v=1:a=0,format=yuv420p[v]" \
  -map "[v]" -c:v libx264 -crf 18 "$ROOT/tests/fixtures/synthetic_cuts.mp4"
echo "$ROOT/tests/fixtures/synthetic_cuts.mp4"
