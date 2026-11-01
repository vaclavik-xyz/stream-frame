# Install

Tested on a Steam Frame (SteamOS, ARM64) and on x86_64 Linux with SteamVR. Requirements:

- Python 3.9+ (no third-party packages)
- `ffmpeg` with libx264
- a C compiler + Vulkan headers (`vulkan/vulkan.h`, `vulkan/vk_layer.h`)
- SteamVR/SteamVR-compatible compositor **started by systemd** (`steamvr.service`), so the layer can be
  enabled through a drop-in

## One shot

```bash
git clone https://github.com/<you>/stream-frame && cd stream-frame
./scripts/install.sh              # add --dry-run to see the plan first
```

The script builds the layer, installs everything into `~/stream-frame`, registers the layer manifest in
`~/.local/share/vulkan/implicit_layer.d/streamframe.json`, writes
`~/.config/systemd/user/steamvr.service.d/50-streamframe.conf` to switch capture on, downloads MediaMTX for
your architecture, installs both user units and enables them.

Then restart SteamVR once (the compositor picks the layer up at start), put the headset on, and open
`http://<host>:8101/`.

## Manual

```bash
make layer                                     # → build/libvrcap.so
install -Dm755 build/libvrcap.so      ~/stream-frame/layer/libvrcap.so
install -Dm644 layer/vrcap_layer.json ~/.local/share/vulkan/implicit_layer.d/streamframe.json
# edit library_path in that JSON if you did not use the installer (it is relative by default)
install -Dm755 server/streamframe.py  ~/stream-frame/server/streamframe.py
install -Dm644 server/settings.py     ~/stream-frame/server/settings.py   # imported by the daemon
install -Dm644 server/gaze.py         ~/stream-frame/server/gaze.py       # imported by the daemon
install -Dm644 server/vrstreamd.py    ~/stream-frame/server/vrstreamd.py  # only for units that still say vrstreamd
install -Dm644 server/web/index.html  ~/stream-frame/server/web/index.html
install -Dm644 systemd/*.service      ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now streamframe.service   # + mediamtx.service for the low-latency path
```

### Switching capture on/off inside SteamVR

Capture is opt-in through the environment of the compositor:

```ini
# ~/.config/systemd/user/steamvr.service.d/50-streamframe.conf
[Service]
Environment=STREAMFRAME_CAP=1
Environment=STREAMFRAME_CAP_W=1440      # capture width per eye; height follows the aspect ratio
Environment=STREAMFRAME_CAP_FPS=60      # cap on captured frames
Environment=STREAMFRAME_CAP_SHM=/streamframe_frames
```

`scripts/capture-off.sh` flips `STREAMFRAME_CAP_OFF=1` and stops the streamer without touching a running
VR session. `DISABLE_GAMESCOPE_WSI=1` is sometimes needed in the same drop-in when gamescope and the
layer fight over presentation — add it only if you see a black capture.

## Running the server

```bash
python3 server/streamframe.py --port 8101 --bitrate 4M --layout mono-flat
python3 server/streamframe.py --help
```

Useful flags: `--rtsp-url ''` (HLS only), `--token <secret>` (require a token on every route except
`/healthz`), `--webrtc-url http://<host>:8889/vr` (shows a "low latency" button in the player),
`--hls-time`, `--hls-window`, `--idle`, `--vf` (raw ffmpeg filter chain), `-v`.

## Verify

```bash
./scripts/smoke-test.sh <host> 8101
```

## Uninstall

```bash
systemctl --user disable --now streamframe.service mediamtx.service
rm ~/.config/systemd/user/{streamframe,mediamtx}.service
rm ~/.config/systemd/user/steamvr.service.d/50-streamframe.conf ~/.config/systemd/user/steamvr.service.d/99-streamframe-off.conf
rm ~/.local/share/vulkan/implicit_layer.d/streamframe.json
rm -rf ~/stream-frame
systemctl --user daemon-reload
```

## Exposure and security

By default the page is reachable by anyone who can route to the port. On a home LAN behind NAT that is
usually what you want; on a shared or tunnelled network start it with `--token` (or `STREAMFRAME_TOKEN`),
which requires `?token=…`, a bearer header, or the cookie the page sets on first load.

There is no input path and no control channel: the stream is read-only by construction.

## Without the Vulkan layer

If you would rather not install a layer, point the daemon at SteamVR's own headset-view node — no restart,
nothing to uninstall, half the per-eye resolution:

```bash
mkdir -p ~/.config/systemd/user/streamframe.service.d
printf '[Service]\nEnvironment=STREAMFRAME_SOURCE=v4l2:/dev/video99\n' \
  > ~/.config/systemd/user/streamframe.service.d/20-source.conf
systemctl --user daemon-reload && systemctl --user restart streamframe.service
```

Skip `scripts/install.sh`'s layer registration in that case; only the daemon, ffmpeg and (optionally)
MediaMTX are needed.

## Sound (optional)

The layer only sees the present queue, which carries no audio, so sound comes from a second tap: the
PipeWire/Pulse **monitor** of the sink the machine plays into. Off by default — a stream that suddenly
requests a capture device is a worse surprise than a silent one.

```bash
pactl list short sinks                      # find the sink the headset/app plays into
mkdir -p ~/.config/systemd/user/streamframe.service.d
printf '[Service]\nEnvironment=STREAMFRAME_AUDIO=auto\n' \
  > ~/.config/systemd/user/streamframe.service.d/20-audio.conf
systemctl --user daemon-reload && systemctl --user restart streamframe.service
```

`auto` means `<default sink>.monitor` and is resolved at every encoder start, so it follows the sink as
long as the default is the right one. When the headset moves the default elsewhere, pin it instead:
`Environment=STREAMFRAME_AUDIO=<sink>.monitor`. For a manual run the same value works as `--audio`.

Audio is AAC 128 kb/s stereo in both paths: HLS and RTSP (VLC/mpv) play it, the browser WebRTC path does
not — browsers do not accept AAC over WHEP, so use HLS there.
