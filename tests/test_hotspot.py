"""The hotspot view is read-only: it parses what SteamOS left in /var/run and never touches the network."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import hotspot  # noqa: E402

STEAMOS_CONF = """interface=wlanap
driver=nl80211
ctrl_interface=/var/run/hostapd
ssid=frame_302A795C
ignore_broadcast_ssid=1
hw_mode=a
channel=37
wpa=2
wpa_key_mgmt=SAE
wpa_pairwise=CCMP
sae_password=correct horse
ieee80211w=2
ap_max_inactivity=10
"""


def fake_run(ip="wlanap  UP  10.35.78.1/24 fe80::1/64", stations="",
             info="channel 37 (6135 MHz), width: 160 MHz, center1: 6135 MHz"):
    def run(args):
        if args[0] == "ip":
            return ip
        return info if args[-1] == "info" else stations
    return run


def test_parse_conf_reads_the_fields_a_client_needs():
    parsed = hotspot.parse_conf(STEAMOS_CONF)
    assert parsed["ssid"] == "frame_302A795C"
    assert parsed["password"] == "correct horse"      # SAE password, not wpa_passphrase
    assert parsed["security"] == "WPA3"
    assert parsed["hidden"] is True                   # ignore_broadcast_ssid=1
    assert parsed["channel"] == 37


def test_parse_conf_calls_a_plain_wpa2_network_wpa2():
    parsed = hotspot.parse_conf("ssid=home\nchannel=6\nwpa=2\nwpa_key_mgmt=WPA-PSK\nwpa_passphrase=abc\n")
    assert parsed["security"] == "WPA2" and parsed["hidden"] is False


def test_parse_conf_calls_an_open_network_open():
    parsed = hotspot.parse_conf("ssid=free\nchannel=44\n")
    assert parsed["security"] == "open" and parsed["password"] == ""


def test_band_comes_from_the_frequency_not_the_channel_number():
    # 37 is both a 5 GHz and a 6 GHz channel; only the frequency settles it
    assert hotspot.band_of(6135) == "6 GHz"
    assert hotspot.band_of(5180) == "5 GHz"
    assert hotspot.band_of(2412) == "2.4 GHz"
    assert hotspot.band_of(0) == ""


def test_state_is_unavailable_without_a_conf(tmp_path):
    info = hotspot.state(conf=str(tmp_path / "missing.conf"), fresh=True)
    assert info["available"] is False and info["ssid"] == "" and info["address"] == ""


def test_state_reports_the_address_and_who_is_connected(tmp_path):
    conf = tmp_path / "softap_hostapd.conf"
    conf.write_text(STEAMOS_CONF)
    stations = "Station aa:bb:cc:dd:ee:ff (on wlanap)\n\tsignal:  -58 [-62, -60] dBm\n" \
               "Station 11:22:33:44:55:66 (on wlanap)\n\tsignal:  -44 [-50, -46] dBm\n"
    info = hotspot.state(conf=str(conf), run=fake_run(stations=stations), fresh=True)
    assert info["available"] is True
    assert info["address"] == "10.35.78.1"        # prefix stripped, link-local ignored
    assert info["clients"] == 2 and info["signal"] == -44
    assert info["freq"] == 6135 and info["band"] == "6 GHz"


def test_state_falls_back_to_unavailable_when_the_interface_has_no_address(tmp_path):
    conf = tmp_path / "c.conf"
    conf.write_text(STEAMOS_CONF)
    info = hotspot.state(conf=str(conf), run=fake_run(ip="wlanap  DOWN "), fresh=True)
    assert info["ssid"] == "frame_302A795C" and info["available"] is False


def test_summary_builds_the_url_the_qr_payload_and_the_mdns_name(tmp_path):
    conf = tmp_path / "c.conf"
    conf.write_text(STEAMOS_CONF)
    info = hotspot.summary(8101, conf=str(conf), run=fake_run(), fresh=True)
    assert info["url"] == "http://10.35.78.1:8101/"
    assert info["mdns"].startswith("http://") and info["mdns"].endswith(".local:8101/")
    # iOS and Android both expect T:WPA for WPA2 and WPA3 alike
    assert info["wifi_payload"] == "WIFI:T:WPA;S:frame_302A795C;P:correct horse;H:true;;"


def test_summary_leaves_the_payload_empty_for_an_open_network(tmp_path):
    conf = tmp_path / "c.conf"
    conf.write_text("ssid=free\nchannel=6\n")
    info = hotspot.summary(8101, conf=str(conf), run=fake_run(), fresh=True)
    assert info["available"] is True and info["wifi_payload"] == ""


def test_state_caches_between_calls(tmp_path):
    conf = tmp_path / "c.conf"
    conf.write_text(STEAMOS_CONF)
    calls = []

    def counting(args):
        calls.append(args[0])
        return fake_run()(args)

    hotspot.state(conf=str(conf), run=counting, fresh=True)
    hotspot.state(conf=str(conf), run=counting)
    assert calls.count("iw") == 2                      # info + station dump, once; the second call was cached
