#!/usr/bin/env bash
# Let clients on the headset's own hotspot reach the internet through the headset.
#
# SteamOS ships the hotspot in firewalld's nm-shared zone with `masquerade: no` and `forward: no`, so a
# phone that joins can reach the player and nothing else — iOS then complains the network has no
# internet and quietly prefers cellular. This turns the headset into an ordinary NAT router for that
# zone: the phone's traffic leaves through the headset's own uplink. The stream is unaffected either
# way; this only decides what else the phone can do while it is joined.
#
#   sudo scripts/hotspot-share.sh          # share the uplink with hotspot clients
#   sudo scripts/hotspot-share.sh --undo   # back to headset-only
#
# It edits firewalld's permanent configuration, so it survives reboots and SteamOS updates.

set -euo pipefail

ZONE=nm-shared
POLICY=streamframe-hotspot
POOL=10.35.78.0/24

if [ "$(id -u)" -ne 0 ]; then
  echo "run me with sudo — I change the firewall" >&2
  exit 1
fi

if ! command -v firewall-cmd >/dev/null 2>&1; then
  echo "firewalld is not here; nothing to do" >&2
  exit 1
fi

if [ "${1:-}" = "--undo" ]; then
  firewall-cmd --permanent --delete-policy="$POLICY" 2>/dev/null || true
  firewall-cmd --permanent --zone="$ZONE" --remove-masquerade 2>/dev/null || true
  firewall-cmd --reload
  echo "hotspot clients are back to reaching the headset only"
  exit 0
fi

# source NAT: packets leaving through the uplink carry the headset's address
firewall-cmd --permanent --zone="$ZONE" --add-masquerade >/dev/null

# ... and a policy that actually lets them be forwarded; a zone alone does not open that path
firewall-cmd --permanent --new-policy="$POLICY" >/dev/null 2>&1 || true
firewall-cmd --permanent --policy="$POLICY" --add-ingress-zone="$ZONE" >/dev/null
firewall-cmd --permanent --policy="$POLICY" --add-egress-zone=ANY >/dev/null
firewall-cmd --permanent --policy="$POLICY" --set-target=ACCEPT >/dev/null
firewall-cmd --reload >/dev/null

echo "forwarding:   $(sysctl -n net.ipv4.ip_forward)"
echo "masquerade:   $(firewall-cmd --zone="$ZONE" --query-masquerade && echo on || echo off)"
echo "policy:       $(firewall-cmd --permanent --policy="$POLICY" --list-all 2>/dev/null | tr '\n' ' ')"
echo
echo "check it from a client on the hotspot ($POOL):"
echo "  curl -s -m 5 -o /dev/null -w '%{http_code}\\n' http://1.1.1.1   # 301 means the uplink is shared"
