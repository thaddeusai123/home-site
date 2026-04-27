"""
Govee LAN API — UDP, no cloud, no auth.

Spec: https://app-h5025.govee.com/user-manual/wlan-guide

  scan  out: 239.255.255.250:4001 (multicast)
  reply in:  device → :4002 (we listen on this local port)
  cmd   out: <device_ip>:4003 (unicast)

Devices need *LAN Control* enabled in the Govee app (per-device setting)
to respond to scans and accept commands. Bypasses the cloud's stale
`online` flag entirely — works whenever the device is reachable on the
same broadcast/multicast domain as the Pi.
"""
from __future__ import annotations

import json
import socket
import time

MCAST_ADDR = "239.255.255.250"
SCAN_PORT  = 4001
RECV_PORT  = 4002
CMD_PORT   = 4003

DEFAULT_DISCOVER_TIMEOUT = 2.5


class GoveeLanError(RuntimeError):
    pass


def discover(timeout: float = DEFAULT_DISCOVER_TIMEOUT) -> list[dict]:
    """Multicast scan for LAN-Control-enabled Govee devices on the local
    subnet. Returns [{ip, device, sku}] for everything that replies
    within `timeout` seconds. Empty list is a normal result."""
    found: dict[str, dict] = {}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", RECV_PORT))
    except OSError as e:
        sock.close()
        raise GoveeLanError(f"could not bind UDP {RECV_PORT}: {e}") from e

    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

    payload = json.dumps({
        "msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}
    }).encode()
    try:
        sock.sendto(payload, (MCAST_ADDR, SCAN_PORT))
    except OSError as e:
        sock.close()
        raise GoveeLanError(f"scan send failed: {e}") from e

    deadline = time.time() + timeout
    sock.settimeout(0.4)
    try:
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            try:
                j = json.loads(data.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            d = (((j or {}).get("msg") or {}).get("data")) or {}
            device = d.get("device")
            if not device:
                continue
            found[device] = {
                "ip": d.get("ip") or addr[0],
                "device": device,
                "sku": d.get("sku"),
            }
    finally:
        sock.close()
    return list(found.values())


def _send(ip: str, cmd: str, data: dict) -> None:
    payload = json.dumps({"msg": {"cmd": cmd, "data": data}}).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(payload, (ip, CMD_PORT))
    except OSError as e:
        raise GoveeLanError(f"send to {ip}:{CMD_PORT}: {e}") from e
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Convenience setters (cap-instance-shaped, so the route can dispatch by name)
# ---------------------------------------------------------------------------

def set_power(ip: str, on: bool) -> None:
    _send(ip, "turn", {"value": 1 if on else 0})


def set_brightness(ip: str, pct: int) -> None:
    pct = max(1, min(100, int(pct)))
    _send(ip, "brightness", {"value": pct})


def set_color_rgb(ip: str, rgb_int: int) -> None:
    n = int(rgb_int) & 0xFFFFFF
    r, g, b = (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF
    # colorwc takes both color and CT — CT=0 means "use color".
    _send(ip, "colorwc", {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0})


def set_color_temp(ip: str, kelvin: int) -> None:
    _send(ip, "colorwc", {"color": {"r": 0, "g": 0, "b": 0},
                          "colorTemInKelvin": int(kelvin)})


# Map (capability_type, instance) → callable(ip, value).
# Returns None if the capability isn't LAN-controllable here; the route
# falls back to the cloud API for those.
def lan_setter(cap_type: str, instance: str):
    if instance == "powerSwitch":
        return lambda ip, v: set_power(ip, bool(int(v)))
    if instance == "brightness":
        return lambda ip, v: set_brightness(ip, int(v))
    if instance == "colorRgb":
        return lambda ip, v: set_color_rgb(ip, int(v))
    if instance == "colorTemperatureK":
        return lambda ip, v: set_color_temp(ip, int(v))
    return None
