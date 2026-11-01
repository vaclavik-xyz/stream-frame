# Streaming over the headset's own Wi-Fi

The headset does not need a router between it and the phone. SteamOS runs a hotspot of its own, next to
its normal Wi-Fi connection, and the player already answers on it — so a phone, a tablet or a deck can
join the headset and pull the stream straight off it.

Nothing in this project starts that hotspot. It reads what SteamOS is doing and hands a phone the two
codes it needs.

## What SteamOS runs

`softapmanager.service` writes `/var/run/softap_hostapd.conf`, starts `hostapd` on its own interface and
gives that interface an address with its own `dnsmasq`:

| piece | what it is |
| --- | --- |
| interface | `wlanap`, next to the station link on `wlan0` |
| address | `10.35.78.1/24`, DHCP pool `10.35.78.20–30` |
| band | 6 GHz, 160 MHz (the station stays on 5 GHz) — both at once on this radio |
| security | WPA3-SAE, hidden SSID |

Both links live at the same time because the radio is Wi-Fi 7: an AP and a station on different bands is
exactly what it is built for. The picture does not travel through your router, and the headset keeps its
own uplink to the PC.

## Joining

Open the player, press `s`, and use **Phone → show the joining code**. Two codes appear:

1. the first one joins the Wi-Fi (hidden network, passphrase included),
2. the second one opens the stream at the address the hotspot handed out.

The panel also shows the plain URL and the passphrase, for anything without a camera.

The URL is printed by the daemon from the live interface, so it follows whatever address the hotspot
gets — no hard-coded `10.35.78.x` anywhere.

## What you get, and what you do not

- **Throughput**: 160 MHz on 6 GHz, 2×2. The stream needs 4 Mbit/s; the link has hundreds.
- **Latency**: one hop instead of two, so usually better than going through the router.
- **No internet**: SteamOS puts the AP in firewalld's `nm-shared` zone with `forward: no` and
  `masquerade: no`. Clients reach the headset and nothing else. iOS and Android will say the network has
  no internet — the stream still works, but the phone may prefer cellular for its own traffic.
  `scripts/hotspot-share.sh` makes the headset route that traffic too, if you want it.
- **Idle clients**: `ap_max_inactivity=10` in the SteamOS config. A page that keeps polling `/status`
  never goes idle; a paused tab may be dropped from the Wi-Fi and have to rejoin.
- **Hidden SSID**: you have to type the name, or scan the code.

## Devices that cannot see it

6 GHz is newer than most hardware. iPhone 15 Pro and later, Wi-Fi 6E/7 laptops and the Steam Deck OLED
can see it. An older iPhone, a Steam Deck LCD, or anything Wi-Fi 5 **cannot**, because the AP is not
transmitting on a band those radios have.

That is a SteamOS decision, not this project's, and it is not fixable from the player: putting the AP on
5 GHz would put it on the same channel as the station link, and the two would then share airtime
(roughly half the throughput, still far more than the stream uses). If you need that, change the channel
in SteamOS's softap configuration and restart `softapmanager` — the player will follow, since the band
and channel it shows come from the interface itself.

## Endpoint

`GET /api/hotspot` returns what the panel draws:

```json
{
  "available": true,
  "ssid": "frame_XXXXXX",
  "password": "<the AP passphrase>",
  "security": "WPA3",
  "hidden": true,
  "channel": 37,
  "freq": 6135,
  "band": "6 GHz",
  "address": "10.35.78.1",
  "clients": 0,
  "signal": null,
  "url": "http://10.35.78.1:8101/",
  "mdns": "http://frame.local:8101/",
  "wifi_payload": "WIFI:T:WPA;S:frame_XXXXXX;P:...;H:true;;"
}
```

It is behind the same token as `/api/settings`, because it carries the AP passphrase. If the token is
empty the page is open anyway — see `docs/settings.md` on why running with `--token` is worth it on a
network you do not own.
