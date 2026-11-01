# Security

Stream Frame is built to run on your own network, between machines you own. It
has no account system, no cloud component and no outbound connections: nothing
leaves the LAN unless you point something at it.

## What it listens on

- `0.0.0.0:8101` — the HTTP server: player page, HLS segments, `/status`,
  `/api/settings`, `/api/clip`, recordings, gaze feed.
- `0.0.0.0:8554` and `:8889` — MediaMTX, when you run the RTSP/WebRTC path:
  RTSP for mpv/VLC, WebRTC for browsers.
- `127.0.0.1:9997` — the MediaMTX control API, on loopback only.

## Authentication

There is none by default. That is a deliberate trade: the tool exists to get a
picture from one room to another in under a second, and a password prompt on a
TV gets in the way.

If you pass `--token` (or set `TOKEN` in the environment), every request must carry it,
either as `?token=...` or in the `streamframe_token` cookie the page sets for
you. Three routes stay open on purpose: `/wake` (MediaMTX calls it to start the
encoder on demand), `/qr.js` (a `<script src>` cannot carry a header) and
`/healthz`. When no token is configured, nothing is gated at all.

Be aware of the limits of that: an HTTP token is sent in cleartext, it shows up
in access logs and in browser history, and the cookie is not marked `Secure`
because the server speaks plain HTTP. Treat it as a lock on a door inside the
house, not as a fence.

## Where it is safe to run

- A home LAN or a private tailnet.
- A phone hotspot with a password you control.

Do not expose it to the internet. There is no rate limiting, no TLS, and the
media paths are not designed to be attacked.

## What it does with your data

- Recordings and clips are written wherever you point `--record` and the HLS
  directory; they are never uploaded.
- The gaze feed is a local shared-memory read, published over SSE and optionally
  appended to a CSV next to the video.
- Wi-Fi credentials: the hotspot page reads the access point's configuration
  file to render a QR code for *you* to scan. It never writes that file, never
  starts or reconfigures the access point, and the password is not stored
  anywhere by this project.
- Nothing phones home. There is no telemetry.

## Reporting a problem

Mail <filip@vaclavik.xyz> with what you did, what happened, and the version
(`git describe --tags`). Please do not open a public issue for anything that
would let someone on the same network take over a machine — I will confirm and
credit you once a fix ships.
