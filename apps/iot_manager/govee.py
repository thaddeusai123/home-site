"""
Govee Developer API v2 client (openapi.api.govee.com).

The v2 router API uniformly covers lights, plugs, sensors, and the water-leak
hub system, returning a list of capabilities per device. We flatten the
common ones (online/power/brightness/color/battery/leak) for the UI and
pass the rest through as `raw`.

Key generation: Govee app → Profile → About Us → Apply for API Key.
"""
from __future__ import annotations

import uuid

import requests

API_BASE = "https://openapi.api.govee.com"
TIMEOUT = 10


class GoveeError(RuntimeError):
    pass


def _headers(api_key: str) -> dict:
    return {
        "Govee-API-Key": api_key,
        "Content-Type": "application/json",
    }


def _post(path: str, api_key: str, payload: dict) -> dict:
    body = {"requestId": str(uuid.uuid4()), "payload": payload}
    try:
        r = requests.post(
            f"{API_BASE}{path}", headers=_headers(api_key),
            json=body, timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise GoveeError(f"network: {e}") from e
    return _check(r, path)


def _get(path: str, api_key: str) -> dict:
    try:
        r = requests.get(
            f"{API_BASE}{path}", headers=_headers(api_key), timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise GoveeError(f"network: {e}") from e
    return _check(r, path)


def _check(r, path: str) -> dict:
    if r.status_code == 401 or r.status_code == 403:
        raise GoveeError("API key rejected (401/403) — regenerate in the Govee app.")
    if r.status_code == 429:
        raise GoveeError("Rate limited by Govee (429) — wait a minute and retry.")
    if r.status_code != 200:
        raise GoveeError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
    try:
        body = r.json()
    except ValueError as e:
        raise GoveeError(f"{path}: non-JSON response") from e
    code = body.get("code")
    if code != 200:
        msg = body.get("message") or body.get("msg") or "unknown error"
        raise GoveeError(f"{path}: code {code}: {msg}")
    return body


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_devices(api_key: str) -> list[dict]:
    body = _get("/router/api/v1/user/devices", api_key)
    return [_summarize_device(d) for d in (body.get("data") or [])]


def get_state(api_key: str, sku: str, device: str) -> dict:
    body = _post(
        "/router/api/v1/device/state", api_key,
        {"sku": sku, "device": device},
    )
    return _summarize_state(body.get("payload") or {})


def control(api_key: str, sku: str, device: str,
            cap_type: str, instance: str, value) -> dict:
    body = _post(
        "/router/api/v1/device/control", api_key,
        {
            "sku": sku, "device": device,
            "capability": {"type": cap_type, "instance": instance, "value": value},
        },
    )
    return body.get("payload") or {}


# ---------------------------------------------------------------------------
# Categorization + state extraction
# ---------------------------------------------------------------------------

# Water-leak hub + sensor SKUs (kit is sometimes labelled "5040", components
# H5054 sensors with hubs in the H504x family). Add more as we encounter them.
_WATER_SKUS = {"H5040", "H5054", "H5055", "H5042", "H5043", "H5044",
               "H5045", "H5046", "H5047", "H5048", "H5049"}


def _classify(d: dict) -> str:
    raw_type = (d.get("type") or "").lower()
    sku = (d.get("sku") or "").upper()
    name = (d.get("deviceName") or "").lower()
    if "light" in raw_type or sku.startswith("H6"):
        return "light"
    if sku in _WATER_SKUS or "leak" in name or "water" in name:
        return "water_sensor"
    if "sensor" in raw_type or sku.startswith("H5"):
        return "sensor"
    return "other"


def _summarize_device(d: dict) -> dict:
    return {
        "sku": d.get("sku"),
        "device": d.get("device"),
        "name": d.get("deviceName") or d.get("sku") or "(unnamed)",
        "type": d.get("type"),
        "category": _classify(d),
        "capabilities": d.get("capabilities") or [],
    }


def _summarize_state(payload: dict) -> dict:
    caps = payload.get("capabilities") or []
    out = {
        "sku": payload.get("sku"),
        "device": payload.get("device"),
        "online": None,
        "powerSwitch": None,
        "brightness": None,
        "colorRgb": None,
        "battery": None,
        "leak": None,
        "raw": caps,
    }
    for c in caps:
        instance = c.get("instance")
        state = c.get("state") or {}
        value = state.get("value")
        if value is None:
            continue
        if instance == "online":
            out["online"] = bool(value)
        elif instance == "powerSwitch":
            try:
                out["powerSwitch"] = int(value)
            except (TypeError, ValueError):
                out["powerSwitch"] = None
        elif instance == "brightness":
            out["brightness"] = value
        elif instance == "colorRgb":
            out["colorRgb"] = value
        elif instance == "battery":
            out["battery"] = value
        elif instance in ("leakEvent", "waterLeakEvent", "leak"):
            out["leak"] = value
    return out
