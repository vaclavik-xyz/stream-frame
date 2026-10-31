# Experiment log

Dead ends are kept here on purpose: each one closed a path, and without the note somebody (me, in a
month) walks into it again.

## 2026-10-20 — `VK_LAYER_LUNARG_screenshot` on vrcompositor

Setup: drop-in `steamvr.service.d/screenshot-layer.conf` with
`VK_INSTANCE_LAYERS=VK_LAYER_LUNARG_screenshot`, `VK_SCREENSHOT_FRAMES=30-60`,
`VK_SCREENSHOT_DIR=/tmp/vr-shots`.

Result:
- **1 frame written** (`/tmp/vr-shots/30.ppm`), but the file is **incomplete** (the process died mid-write)
- the compositor crashed inside **`libvulkan_freedreno.so`** (the Adreno Vulkan driver) — stack trace in
  `journalctl --user -u steamvr.service`
- SteamVR did not come up; `scripts/capture-off.sh` brought VR back (vrserver + compositor fine)
- system integrity: boot slot `rootfs.1 (B)`, `steamapps` untouched

Conclusion: the "off-the-shelf LunarG layer" route is **not stable** on this GPU. Reading the swapchain
image back through `vkCmdCopyImageToBuffer` crashes in freedreno. The next attempt needs a different kind
of read — our own blit into a linear image with explicit barriers, which is what `vrcap.c` ended up doing
— or a different capture architecture altogether.

Verified beforehand on `vkcube`: 15 frames, 6,220,800 B each, non-zero content — so the layer mechanism
works; the problem is specific to the compositor's display swapchain.

## SteamVR mirror window

- `showMirrorView=true`, `mirrorViewGeometry="0 0 1280 720"`, `allowFallbackMirrorWindowLinux=true`
- the compositor no longer logs "Found bad mirror window settings" at start, but **no window of that
  geometry appears**
- the X server only shows `0x2200005 "SteamVR" 1920x1080` (the dashboard/UI surface), and capturing it
  gives black
- open question: does that window carry content while a VR application is actually running? (test with
  the headset on)
