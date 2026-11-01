# Prior art: who else shows, records and streams a VR headset view

Checked 2026-11-01. Star counts and licence come from the GitHub API on that date; quotes come from the
projects' own READMEs or docs. Anything I could not confirm from a first-party source is marked
*unverified*.

Stream Frame is not the first attempt at "see what the headset sees", so this page exists to keep the
positioning honest: what others do better, what nobody does, and what is worth copying.

## Tools that show or record the headset view

- **[Frame Control](https://github.com/saphid/steam-frame)** (MIT, 127★, active 2026-10-31) — the
  end-to-end desktop app for the Steam Frame (macOS/Windows/Linux): games, sideloaded F-Droid apps,
  files, clipboard, screenshots, power, and a headset view. Its own docs describe the view: *"Live is
  720p video at about 30 fps: `ffmpeg` on the Frame encodes SteamVR's headset-view device
  (`/dev/video99`) to H.264 over SSH, and the page decodes it with WebCodecs. Live video is one eye;
  Capture still gets both."* Nothing is installed on the headset.
- **[Stream Frame](https://streamframe.app/)** (free app, macOS/Windows/Linux + a native Flatpak on the
  headset) — live preview with **no audio**, MP4 recording written on the headset and downloaded,
  screenshots, over SSH (`frame.local` or an IP), optionally through Tailscale.
- **[FrameMate](https://github.com/nailuj05/framemate)** (GPL-3.0, 121★) — mirrors the headset to a
  phone with hardware encoding and TLS pinned to the headset's own key; also battery, downloads and
  phone notifications in VR.
- **Steam Link** — Valve's own host-to-headset streaming, the other direction.
- **framecorder** (`framecorder.coah80.com`, closed) — a recorder that runs on the headset itself.

What all of these have in common: the picture ends up inside *their* app. None of them hands you a URL
that VLC, mpv, a TV, or a browser tab can open.

## Tools that capture frames at the graphics API level

- **[obs-vkcapture](https://github.com/nowrep/obs-vkcapture)** (GPL-2.0, 826★) — the closest sibling:
  a Vulkan/OpenGL layer plus an OBS plugin that captures an application's frames in real time, with low
  overhead and Wayland support. Same idea as `layer/vrcap.c`, different consumer.
- **[vkBasalt](https://github.com/DadSchoorse/vkBasalt)** (Zlib, 1571★) — a Vulkan post-processing layer;
  proof that an implicit layer can read and rewrite the framebuffer, in the wild.
- **`VK_LAYER_LUNARG_screenshot`** — LunarG's official screenshot layer. Tried on the Frame and
  abandoned: one partial frame, then a crash inside `libvulkan_freedreno.so` (see
  [experiments.md](experiments.md)).
- **`steamvr-v4l2cam`** — not third-party at all: SteamVR's own service that copies the composited
  headset view into the v4l2loopback node `/dev/video99` (1920×1080). Both Frame Control and Stream
  Frame read it, and so does `--source v4l2:/dev/video99`.
- **xdg-desktop-portal + PipeWire** — the standard Wayland capture path, and useless here: a VR
  compositor is not a desktop window.

## Stacks that stream VR between machines

- **[ALVR](https://github.com/alvr-org/ALVR)** (MIT, 8037★) — PC to standalone headset over Wi-Fi, on top
  of SteamVR, GPU encoding, its own protocol. Reported glass-to-glass latency is in the 50–100 ms class;
  users in r/virtualreality_linux quote ~74 ms (community measurement, *unverified*).
- **[WiVRn](https://github.com/WiVRn/WiVRn)** (GPL-3.0, 1829★) — the Linux-native counterpart, built on
  Monado + OpenComposite instead of SteamVR; the same threads quote ~60–65 ms (*unverified*).
- **[Monado](https://gitlab.freedesktop.org/monado/monado)** — the open-source OpenXR runtime both of the
  above lean on.

These are **interactive** streaming stacks: a headset client decodes into the display pipeline, frames
are timed to the panel, and the transport is tuned for tens of milliseconds. That is the opposite trade
from Stream Frame: any player, no client, seconds of buffer allowed, and a picture you can send to a TV.

## Transport

- **[MediaMTX](https://github.com/bluenviron/mediamtx)** (MIT, 20567★) — SRT, WebRTC (WHIP/WHEP), RTSP,
  RTMP, LL-HLS, MPEG-TS, RTP; `runOnDemand`, recording, playback.
- **[go2rtc](https://github.com/AlexxIT/go2rtc)** (MIT, 14549★) — protocol bridge popular in Home
  Assistant.
- The constraint that decides the audio story everywhere: browsers require Opus in WebRTC and do not
  accept AAC, so a browser WHEP player is silent unless a second, Opus-encoded audio track is added.
  HLS and RTSP carry AAC fine — which is exactly why Stream Frame's sound works in VLC/mpv/HLS and not in
  the WebRTC page.

## Eye tracking on the Frame

- **[frameeyeosc](https://github.com/konsti219/frameeyeosc)** (MIT, 74★) — reads gaze **and eye
  openness** and sends them as OSC (default `127.0.0.1:9000`). Its README is the important find: *"this
  data is only exposed in an internal shared-memory object (`/dev/shm/eye-server.mmap`), and not via any
  public APIs."*
- **[vrcft-steam-frame](https://github.com/hakumaguro/vrcft-steam-frame)** (MIT, 11★) — per-eye gaze,
  blinks and winks into VRChat through
  [VRCFaceTracking](https://github.com/benaclejames/VRCFaceTracking) (Apache-2.0, 918★).
- **Raw eye-camera images: nobody has them.** HTC's official position on the Vive Pro Eye is explicit
  and repeated: *"Access to RAW imagery is not available with the SRAnipal SDK"*, the Tobii backend
  *"do not allow for images of the eyes to be collected or transmitted"*
  and Tobii's own XR SDK, licensed separately, is where the lower-level access lives
  ([forum.htc.com](https://forum.htc.com/topic/12922-get-stream-video-from-eye-tracking-cameras-htc-vive-pro-eye),
  [forum.vive.com](https://forum.vive.com/topic/6726-get-an-image-of-an-eye)). On the Frame the runtime
  publishes gaze and openness, not pixels. Raw eye images are biometric data under GDPR Art. 9, which is
  a good part of why vendors gate them.

## Where Stream Frame sits

- **Not** an interactive VR streaming stack like ALVR/WiVRn: RTSP/WebRTC lands at roughly 0.3–0.8 s here,
  an order of magnitude above a panel-synchronised client. It is for watching, recording and sharing,
  not for playing.
- **Not** an app: no viewer to install, no SSH in the path, no vendor account. One URL, any player.
- **Wider** than the apps above in two ways: full per-eye resolution through the Vulkan layer (1440 px
  per eye instead of the 960×1080 half of `/dev/video99`), and 60 fps with audio.
- **Narrower** in one: no on-device recording, no metadata panels, no file transfer. Those exist and are
  better elsewhere.

## What is worth copying

1. **Discovery.** Frame Control finds a headset at `frame.local` and explains each connection step when
   it fails. Stream Frame still expects an IP on the command line.
2. **A gaze sidecar.** Reading `/dev/shm/eye-server.mmap` (the frameeyeosc approach) and publishing gaze
   with a timestamp next to the video would let a player draw a reticle, and would be the first
   open implementation that keeps gaze and picture in one timeline.
3. **A Flatpak.** Stream Frame ships as a Flatpak installed from Discover on the headset; the daemon is
   pure Python and would package easily.
4. **Recording.** Both apps record; Stream Frame only streams. A `-f segment` branch into an MP4 ring would
   cost one more tee slave.
