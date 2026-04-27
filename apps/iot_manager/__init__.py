"""
IoT Manager — local hub for smart-home gear.

Each ecosystem (Govee today; Zigbee/Z-Wave/etc. later) lives in its own
section. State that needs to persist across restarts (API keys, etc.) goes
in the shared `iot_prefs` table.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from . import govee, govee_lan

LAN_PREF_PREFIX = "govee_lan_"

bp = Blueprint("iot_manager", __name__, url_prefix="/iot")

APP_META = {
    "slug": "iot-manager",
    "name": "IoT Manager",
    "tagline": "Local hub for smart-home gear — Govee lights and sensors, more to come.",
    "icon": "\U0001f4e1",
    "url_endpoint": "iot_manager.index",
    "status_endpoint": None,
}


def _db():
    import app as _app
    return _app


def _get_pref(key: str) -> str | None:
    db = _db()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT value FROM iot_prefs WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else None


def _set_pref(key: str, value: str) -> None:
    db = _db()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO iot_prefs(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _del_pref(key: str) -> None:
    db = _db()
    with db.connect() as conn:
        conn.execute("DELETE FROM iot_prefs WHERE key = ?", (key,))


def _get_lan_ips() -> dict[str, str]:
    """Return {device_id: ip} for every cached LAN entry."""
    db = _db()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT key, value FROM iot_prefs WHERE key LIKE ?",
            (LAN_PREF_PREFIX + "%",),
        ).fetchall()
    return {row["key"][len(LAN_PREF_PREFIX):]: row["value"] for row in rows}


def _set_lan_ip(device: str, ip: str) -> None:
    _set_pref(LAN_PREF_PREFIX + device, ip)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    return render_template("iot_manager/index.html")


@bp.route("/govee")
def govee_page():
    return render_template(
        "iot_manager/govee.html",
        api_key_set=bool(_get_pref("govee_api_key")),
    )


# ---------------------------------------------------------------------------
# Govee — API key management
# ---------------------------------------------------------------------------

@bp.route("/govee/api-key", methods=["POST"])
def save_api_key():
    body = request.get_json(silent=True) or request.form
    key = (body.get("api_key") or "").strip()
    if not key:
        return jsonify({"error": "api_key required"}), 400
    _set_pref("govee_api_key", key)
    return jsonify({"ok": True})


@bp.route("/govee/api-key", methods=["DELETE"])
def clear_api_key():
    _del_pref("govee_api_key")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Govee — devices, state, control
# ---------------------------------------------------------------------------

def _require_key():
    key = _get_pref("govee_api_key")
    if not key:
        return None, (jsonify({"error": "Govee API key not set"}), 400)
    return key, None


@bp.route("/govee/api/devices")
def list_devices():
    key, err = _require_key()
    if err:
        return err
    try:
        devs = govee.list_devices(key)
    except govee.GoveeError as e:
        return jsonify({"error": str(e)}), 502
    lan_ips = _get_lan_ips()
    for d in devs:
        d["lan_ip"] = lan_ips.get(d.get("device") or "")
    return jsonify(devs)


@bp.route("/govee/api/lan/discover", methods=["POST"])
def lan_discover():
    """Multicast scan the local subnet. Caches discovered IPs in iot_prefs
    keyed by device MAC so subsequent control calls skip the cloud."""
    try:
        found = govee_lan.discover()
    except govee_lan.GoveeLanError as e:
        return jsonify({"error": str(e)}), 500
    for entry in found:
        if entry.get("device") and entry.get("ip"):
            _set_lan_ip(entry["device"], entry["ip"])
    return jsonify({"ok": True, "found": found, "count": len(found)})


@bp.route("/govee/api/state")
def get_state():
    key, err = _require_key()
    if err:
        return err
    sku = request.args.get("sku")
    device = request.args.get("device")
    if not sku or not device:
        return jsonify({"error": "sku and device required"}), 400
    try:
        return jsonify(govee.get_state(key, sku, device))
    except govee.GoveeError as e:
        return jsonify({"error": str(e)}), 502


@bp.route("/govee/api/control", methods=["POST"])
def control():
    body = request.get_json(force=True)
    try:
        sku      = body["sku"]
        device   = body["device"]
        cap_type = body["type"]
        instance = body["instance"]
        value    = body["value"]
    except KeyError as e:
        return jsonify({"error": f"missing field: {e.args[0]}"}), 400

    # Prefer LAN when we have a cached IP and the capability is LAN-supported.
    # Govee's cloud refuses to forward commands when its `online` flag is
    # stale-false, even if the device is happily reachable on the LAN.
    lan_ip = _get_lan_ips().get(device)
    setter = govee_lan.lan_setter(cap_type, instance)
    if lan_ip and setter:
        try:
            setter(lan_ip, value)
            return jsonify({"ok": True, "via": "lan", "ip": lan_ip})
        except govee_lan.GoveeLanError as e:
            # LAN failed — fall through to cloud rather than giving up.
            lan_err = str(e)
        else:
            lan_err = None
    else:
        lan_err = None

    key, err = _require_key()
    if err:
        return err
    try:
        govee.control(key, sku, device, cap_type, instance, value)
    except govee.GoveeError as e:
        msg = f"cloud: {e}"
        if lan_err:
            msg = f"lan: {lan_err}; {msg}"
        return jsonify({"error": msg}), 502
    return jsonify({"ok": True, "via": "cloud"})
