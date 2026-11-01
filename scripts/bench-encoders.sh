#!/bin/sh
# Which encoder can carry the live chain on this SoC? Same filter work, same size,
# 10 s of synthetic input, one encoder per run. -benchmark prints utime+stime, which
# is the number that matters: during a game ffmpeg competes with Beat Saber for 4 cores.
set -u
SRC="-f lavfi -i testsrc2=size=2400x1200:rate=60 -t 10"
VF="crop=1200:1200:0:0,v360=input=fisheye:output=flat:ih_fov=180:iv_fov=180:h_fov=110:v_fov=75:w=1280:h=720"

run() {
    name=$1; shift
    printf '== %s\n' "$name"
    # shellcheck disable=SC2086
    ffmpeg -hide_banner -loglevel error -benchmark $SRC -vf "$VF" "$@" -y "/tmp/bench_$name.mp4" 2>&1 | tail -3
    if [ -f "/tmp/bench_$name.mp4" ]; then
        printf '  out: '
        ffprobe -v error -show_entries stream=codec_name,width,height,pix_fmt -of csv=p=0 "/tmp/bench_$name.mp4" | head -1
    else
        printf '  out: FAILED\n'
    fi
    rm -f "/tmp/bench_$name.mp4"
}

run x264   -c:v libx264 -preset ultrafast -tune zerolatency -b:v 10M -maxrate 10M -pix_fmt yuv420p
run v4l2   -c:v h264_v4l2m2m -b:v 10M -pix_fmt nv12
printf '== formats\n'
ffmpeg -hide_banner -h encoder=h264_v4l2m2m 2>&1 | grep -i -A2 'supported pixel formats' | head -3
