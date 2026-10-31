# Install

Tested on a Steam Frame (SteamOS, ARM64) and on x86_64 Linux with SteamVR. Requirements:

- Python 3.9+ (no third-party packages)
- `ffmpeg` with libx264
- a C compiler + Vulkan headers (`vulkan/vulkan.h`, `vulkan/vk_layer.h`)
- SteamVR/SteamVR-compatible compositor **started by systemd** (`steamvr.service`), so the layer can be
  enabled through a drop-in

## One shot

```bash
git clone https://github.com/<you>/vrstream && cd vrstream
./scripts/install.sh              # add --dry-run to see the plan first
```

The script builds the layer, installs everything into `~/vrstream`, registers the layer manifest in
`~/.local/share/vulkan/implicit_layer.d/vrstream.json`, writes
`~/.config/systemd/user/steamvr.service.d/50-vrstream.conf` to switch capture on, downloads MediaMTX for
your architecture, installs both user units and enables them.

Then restart SteamVR once (the compositor picks the layer up at start), put the headset on, and open
`http://<host>:8101/`.

## Manual

```bash
make layer                                     # → build/libvrcap.so
install -Dm755 build/libvrcap.so      ~/vrstream/layer/libvrcap.so
install -Dm644 layer/vrcap_layer.json ~/.local/share/vulkan/implicit_layer.d/vrstream.json
# edit library_path in that JSON if you did not use the installer (it is relative by default)
install -Dm755 server/vrstreamd.py    ~/vrstream/server/vrstreamd.py
install -Dm644 server/web/index.html  ~/vrstream/server/web/index.html
install -Dm644 systemd/*.service      ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vrstream.service   # + mediamtx.service for the low-latency path
```

### Switching capture on/off inside SteamVR

Capture is opt-in through the environment of the compositor:

```ini
# ~/.config/systemd/user/steamvr.service.d/50-vrstream.conf
[Service]
Environment=VRSTREAM_CAP=1
Environment=VRSTREAM_CAP_W=1440      # capture width per eye; height follows the aspect ratio
Environment=VRSTREAM_CAP_FPS=60      # cap on captured frames
Environment=VRSTREAM_CAP_SHM=/vrstream_frames
```

`scripts/capture-off.sh` flips `VRSTREAM_CAP_OFF=1` and stops the streamer without touching a running
VR session. `DISABLE_GAMESCOPE_WSI=1` is sometimes needed in the same drop-in when gamescope and the
layer fight over presentation — add it only if you see a black capture.

## Running the server

```bash
python3 server/vrstreamd.py --port 8101 --bitrate 4M --layout mono-flat
python3 server/vrstreamd.py --help
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
systemctl --user disable --now vrstream.service mediamtx.service
rm ~/.config/systemd/user/{vrstream,mediamtx}.service
rm ~/.config/systemd/user/steamvr.service.d/50-vrstream.conf ~/.config/systemd/user/steamvr.service.d/99-vrstream-off.conf
rm ~/.local/share/vulkan/implicit_layer.d/vrstream.json
rm -rf ~/vrstream
systemctl --user daemon-reload
```

## Exposure and security

By default the page is reachable by anyone who can route to the port. On a home LAN behind NAT that is
usually what you want; on a shared or tunnelled network start it with `--token` (or `VRSTREAM_TOKEN`),
which requires `?token=…`, a bearer header, or the cookie the page sets on first load.

There is no input path and no control channel: the stream is read-only by construction.

## Sound (optional)

The layer only sees the present queue, which carries no audio, so sound comes from a second tap: the
PipeWire/Pulse **monitor** of the sink the machine plays into. Off by default — a stream that suddenly
requests a capture device is a worse surprise than a silent one.

```bash
pactl list short sinks                      # find the sink the headset/app plays into
mkdir -p ~/.config/systemd/user/vrstream.service.d
printf '[Service]\nEnvironment=VRSTREAM_AUDIO=auto\n' \
  > ~/.config/systemd/user/vrstream.service.d/20-audio.conf
systemctl --user daemon-reload && systemctl --user restart vrstream.service
```

`auto` means `<default sink>.monitor` and is resolved at every encoder start, so it follows the sink as
long as the default is the right one. When the headset moves the default elsewhere, pin it instead:
`Environment=VRSTREAM_AUDIO=<sink>.monitor`. For a manual run the same value works as `--audio`.

Audio is AAC 128 kb/s stereo in both paths: HLS and RTSP (VLC/mpv) play it, the browser WebRTC path does
not — browsers do not accept AAC over WHEP, so use HLS there.
