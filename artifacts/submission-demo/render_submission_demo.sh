#!/bin/zsh
set -euo pipefail

ROOT="/Users/alexanderkrauck/Coding/eventindex"
DEMO="$ROOT/artifacts/submission-demo"
SEGMENTS="$DEMO/segments"
OUT="$DEMO/wasup-chatgpt-app-submission-demo-ios-web.mp4"

CLIP1="/Users/alexanderkrauck/Downloads/ScreenRecording_07-15-2026 08-36-12_1.MP4"
CLIP2="/Users/alexanderkrauck/Downloads/ScreenRecording_07-15-2026 08-38-05_1.MP4"
CLIP3="/Users/alexanderkrauck/Downloads/ScreenRecording_07-15-2026 08-39-34_1.MP4"
CLIP4="/Users/alexanderkrauck/Downloads/ScreenRecording_07-15-2026 08-41-08_1.MP4"
CLIP5="/Users/alexanderkrauck/Downloads/ScreenRecording_07-15-2026 08-41-48_1.MP4"

mkdir -p "$SEGMENTS"

encode_card() {
  local image="$1"
  local duration="$2"
  local output="$3"
  local fade_out
  fade_out=$(awk -v d="$duration" 'BEGIN { printf "%.2f", d - 0.25 }')
  ffmpeg -y -hide_banner -loglevel error \
    -loop 1 -framerate 30 -t "$duration" -i "$image" \
    -vf "fade=t=in:st=0:d=0.25,fade=t=out:st=${fade_out}:d=0.25,format=yuv420p" \
    -an -c:v libx264 -preset fast -crf 20 -r 30 -video_track_timescale 90000 "$output"
}

encode_web_still() {
  local image="$1"
  local duration="$2"
  local output="$3"
  local fade_out
  fade_out=$(awk -v d="$duration" 'BEGIN { printf "%.2f", d - 0.25 }')
  ffmpeg -y -hide_banner -loglevel error \
    -loop 1 -framerate 30 -t "$duration" -i "$image" \
    -vf "scale=1080:608:flags=lanczos,pad=1080:1920:0:656:color=#090909,fade=t=in:st=0:d=0.25,fade=t=out:st=${fade_out}:d=0.25,format=yuv420p" \
    -an -c:v libx264 -preset fast -crf 20 -r 30 -video_track_timescale 90000 "$output"
}

PHONE="scale=-2:1920:flags=lanczos,pad=1080:1920:(ow-iw)/2:0:color=#090909,setsar=1"

encode_card "$DEMO/rendered-cards/00-opening.png" 4.5 "$SEGMENTS/00-opening.mp4"
encode_card "$DEMO/rendered-cards/01-web.png" 3.5 "$SEGMENTS/01-web-card.mp4"
encode_web_still "$DEMO/web/web-wasup-plugin.png" 4.0 "$SEGMENTS/02-web-plugin.mp4"
encode_web_still "$DEMO/web/web-details-top.png" 5.0 "$SEGMENTS/03-web-details.mp4"
encode_web_still "$DEMO/web/web-details.png" 5.0 "$SEGMENTS/04-web-provenance.mp4"

encode_card "$DEMO/rendered-cards/02-search-events.png" 3.5 "$SEGMENTS/05-case1-card.mp4"
ffmpeg -y -hide_banner -loglevel error -i "$CLIP1" -filter_complex \
  "[0:v]trim=start=30:end=40,setpts=(PTS-STARTPTS)/1.25,${PHONE}[a]; \
   [0:v]trim=start=40:end=94.6,setpts=(PTS-STARTPTS)/3.0,${PHONE}[b]; \
   [a][b]concat=n=2:v=1:a=0,fps=30,format=yuv420p[out]" \
  -map "[out]" -an -c:v libx264 -preset fast -crf 20 -video_track_timescale 90000 "$SEGMENTS/06-case1.mp4"

encode_card "$DEMO/rendered-cards/03-details.png" 3.5 "$SEGMENTS/07-case2-card.mp4"
ffmpeg -y -hide_banner -loglevel error -i "$CLIP2" -filter_complex \
  "[0:v]trim=start=28:end=42.5,setpts=(PTS-STARTPTS)/1.5,${PHONE}[a]; \
   [0:v]trim=start=46:end=77.7,setpts=(PTS-STARTPTS)/2.0,${PHONE}[b]; \
   [a][b]concat=n=2:v=1:a=0,fps=30,format=yuv420p[out]" \
  -map "[out]" -an -c:v libx264 -preset fast -crf 20 -video_track_timescale 90000 "$SEGMENTS/08-case2.mp4"

encode_card "$DEMO/rendered-cards/04-calendar.png" 3.5 "$SEGMENTS/09-case3-card.mp4"
ffmpeg -y -hide_banner -loglevel error -i "$CLIP3" -filter_complex \
  "[0:v]trim=start=24:end=48,setpts=(PTS-STARTPTS)/2.0,${PHONE}[a]; \
   [0:v]trim=start=48:end=60.4,setpts=PTS-STARTPTS,${PHONE}[b]; \
   [a][b]concat=n=2:v=1:a=0,fps=30,format=yuv420p[out]" \
  -map "[out]" -an -c:v libx264 -preset fast -crf 20 -video_track_timescale 90000 "$SEGMENTS/10-case3.mp4"

encode_card "$DEMO/rendered-cards/05-search.png" 3.5 "$SEGMENTS/11-case4-card.mp4"
ffmpeg -y -hide_banner -loglevel error -i "$CLIP4" -filter_complex \
  "[0:v]trim=start=0:end=7.8,setpts=(PTS-STARTPTS)/1.2,${PHONE},fps=30,format=yuv420p[out]" \
  -map "[out]" -an -c:v libx264 -preset fast -crf 20 -video_track_timescale 90000 "$SEGMENTS/12-case4.mp4"

encode_card "$DEMO/rendered-cards/06-fetch.png" 3.5 "$SEGMENTS/13-case5-card.mp4"
ffmpeg -y -hide_banner -loglevel error -i "$CLIP5" -filter_complex \
  "[0:v]trim=start=6:end=38,setpts=(PTS-STARTPTS)/2.5,${PHONE}[a]; \
   [0:v]trim=start=38:end=66.3,setpts=(PTS-STARTPTS)/2.0,${PHONE}[b]; \
   [a][b]concat=n=2:v=1:a=0,fps=30,format=yuv420p[out]" \
  -map "[out]" -an -c:v libx264 -preset fast -crf 20 -video_track_timescale 90000 "$SEGMENTS/14-case5.mp4"

encode_card "$DEMO/rendered-cards/07-closing.png" 5.0 "$SEGMENTS/15-closing.mp4"

ffmpeg -y -hide_banner -loglevel error \
  -f concat -safe 0 -i "$DEMO/concat.txt" \
  -c copy -movflags +faststart "$OUT"

ffprobe -v error -show_entries format=duration,size:stream=codec_name,width,height,r_frame_rate \
  -of default=noprint_wrappers=1 "$OUT"
