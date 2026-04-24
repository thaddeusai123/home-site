"""
GPIO Lab — web tool for probing Raspberry Pi GPIO pins.
Fire relays, identify wiring, and drive pin states from the browser.

Pins are claimed lazily: no DigitalOutputDevice is created until the user
explicitly sets / pulses a pin. This avoids accidentally firing an active-low
relay the moment the page loads.
"""

from __future__ import annotations

import threading
import time

from flask import Blueprint, jsonify, render_template, request

bp = Blueprint("gpio_lab", __name__, url_prefix="/gpio-lab")

APP_META = {
    "slug": "gpio-lab",
    "name": "GPIO Lab",
    "tagline": "Probe GPIO pins and fire relays from the browser.",
    "icon": "⚡",
    "url_endpoint": "gpio_lab.index",
    "status_endpoint": None,
}

# BCM GPIO → (physical header pin, special-function note or None)
PIN_MAP = {
    2:  (3,  "I²C1 SDA"),
    3:  (5,  "I²C1 SCL"),
    4:  (7,  None),
    5:  (29, None),
    6:  (31, None),
    7:  (26, "SPI0 CE1"),
    8:  (24, "SPI0 CE0"),
    9:  (21, "SPI0 MISO"),
    10: (19, "SPI0 MOSI"),
    11: (23, "SPI0 SCLK"),
    12: (32, "PWM0"),
    13: (33, "PWM1"),
    14: (8,  "UART TX"),
    15: (10, "UART RX"),
    16: (36, None),
    17: (11, None),
    18: (12, "PCM CLK / PWM0"),
    19: (35, "PCM FS"),
    20: (38, "PCM DIN"),
    21: (40, "PCM DOUT"),
    22: (15, None),
    23: (16, None),
    24: (18, None),
    25: (22, None),
    26: (37, None),
    27: (13, None),
}

# Lazy import — dev machines (Mac) can still load the blueprint and render UI.
_gpio_error: str | None = None
try:
    from gpiozero import DigitalOutputDevice  # type: ignore
    GPIO_AVAILABLE = True
except Exception as e:  # pragma: no cover — depends on host
    DigitalOutputDevice = None  # type: ignore
    GPIO_AVAILABLE = False
    _gpio_error = f"{type(e).__name__}: {e}"

_devices: dict[int, object] = {}
_devices_lock = threading.Lock()


def _db():
    import app as _app
    return _app


def _ensure_device(pin: int, initial_value: bool):
    if not GPIO_AVAILABLE:
        raise RuntimeError(f"GPIO library unavailable ({_gpio_error}). Install gpiozero + lgpio on the Pi.")
    with _devices_lock:
        dev = _devices.get(pin)
        if dev is None:
            dev = DigitalOutputDevice(pin, initial_value=initial_value)
            _devices[pin] = dev
        return dev


def _close_device(pin: int):
    with _devices_lock:
        dev = _devices.pop(pin, None)
    if dev is not None:
        try:
            dev.close()
        except Exception:
            pass


def _close_all():
    with _devices_lock:
        items = list(_devices.items())
        _devices.clear()
    for _, dev in items:
        try:
            dev.close()
        except Exception:
            pass


def _state_for(pin: int):
    with _devices_lock:
        dev = _devices.get(pin)
    if dev is None:
        return None
    try:
        return bool(dev.value)
    except Exception:
        return None


def _labels_from_db() -> dict[int, str]:
    db = _db()
    with db.connect() as conn:
        rows = conn.execute("SELECT pin, label FROM gpio_labels").fetchall()
    return {int(r["pin"]): r["label"] for r in rows}


def _parse_state(raw):
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in {"high", "on", "1", "true"}:
        return True
    if s in {"low", "off", "0", "false"}:
        return False
    return None


def _validate_pin(raw):
    try:
        p = int(raw)
    except (TypeError, ValueError):
        return None
    return p if p in PIN_MAP else None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    return render_template("gpio_lab/index.html")


@bp.route("/api/pins")
def api_list_pins():
    labels = _labels_from_db()
    pins_out = []
    for pin, (phys, special) in sorted(PIN_MAP.items()):
        pins_out.append({
            "pin": pin,
            "physical": phys,
            "special": special,
            "label": labels.get(pin, ""),
            "state": _state_for(pin),
            "claimed": pin in _devices,
        })
    return jsonify({
        "gpio_available": GPIO_AVAILABLE,
        "gpio_error": _gpio_error,
        "pins": pins_out,
    })


@bp.route("/api/pins/<int:pin>/set", methods=["POST"])
def api_set(pin):
    if pin not in PIN_MAP:
        return jsonify({"error": f"unknown pin {pin}"}), 400
    body = request.get_json(force=True, silent=True) or {}
    state = _parse_state(body.get("state"))
    if state is None:
        return jsonify({"error": "state must be 'high' or 'low'"}), 400
    try:
        dev = _ensure_device(pin, initial_value=state)
        dev.value = 1 if state else 0
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "pin": pin, "state": bool(dev.value), "claimed": True})


@bp.route("/api/pins/<int:pin>/pulse", methods=["POST"])
def api_pulse(pin):
    if pin not in PIN_MAP:
        return jsonify({"error": f"unknown pin {pin}"}), 400
    body = request.get_json(force=True, silent=True) or {}
    fire = _parse_state(body.get("fire_state"))
    if fire is None:
        return jsonify({"error": "fire_state must be 'high' or 'low'"}), 400
    try:
        duration_ms = int(body.get("duration_ms", 500))
    except (TypeError, ValueError):
        return jsonify({"error": "duration_ms must be an integer"}), 400
    duration_ms = max(10, min(5000, duration_ms))
    rest = not fire
    try:
        dev = _ensure_device(pin, initial_value=rest)
        dev.value = 1 if rest else 0
        dev.value = 1 if fire else 0
        time.sleep(duration_ms / 1000.0)
        dev.value = 1 if rest else 0
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({
        "ok": True, "pin": pin, "duration_ms": duration_ms,
        "fire_state": "high" if fire else "low",
        "state": bool(dev.value), "claimed": True,
    })


@bp.route("/api/pins/<int:pin>/release", methods=["POST"])
def api_release(pin):
    if pin not in PIN_MAP:
        return jsonify({"error": f"unknown pin {pin}"}), 400
    _close_device(pin)
    return jsonify({"ok": True, "pin": pin, "claimed": False, "state": None})


@bp.route("/api/release-all", methods=["POST"])
def api_release_all():
    _close_all()
    return jsonify({"ok": True})


@bp.route("/api/pins/<int:pin>/label", methods=["POST"])
def api_set_label(pin):
    if pin not in PIN_MAP:
        return jsonify({"error": f"unknown pin {pin}"}), 400
    body = request.get_json(force=True, silent=True) or {}
    label = (body.get("label") or "").strip()
    db = _db()
    with db.connect() as conn:
        if label:
            conn.execute(
                "INSERT INTO gpio_labels (pin, label, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(pin) DO UPDATE SET label = excluded.label, updated_at = excluded.updated_at",
                (pin, label, db.now_iso()),
            )
        else:
            conn.execute("DELETE FROM gpio_labels WHERE pin = ?", (pin,))
    return jsonify({"ok": True, "pin": pin, "label": label})


@bp.route("/api/sweep", methods=["POST"])
def api_sweep():
    if not GPIO_AVAILABLE:
        return jsonify({"error": f"GPIO library unavailable ({_gpio_error})"}), 500
    body = request.get_json(force=True, silent=True) or {}
    raw_pins = body.get("pins")
    if not isinstance(raw_pins, list) or not raw_pins:
        return jsonify({"error": "pins must be a non-empty list"}), 400
    pins: list[int] = []
    for p in raw_pins:
        v = _validate_pin(p)
        if v is None:
            return jsonify({"error": f"unknown pin {p}"}), 400
        if v not in pins:
            pins.append(v)
    fire = _parse_state(body.get("fire_state"))
    if fire is None:
        return jsonify({"error": "fire_state must be 'high' or 'low'"}), 400
    try:
        duration_ms = int(body.get("duration_ms", 500))
        gap_ms = int(body.get("gap_ms", 250))
    except (TypeError, ValueError):
        return jsonify({"error": "duration_ms / gap_ms must be integers"}), 400
    duration_ms = max(10, min(5000, duration_ms))
    gap_ms = max(0, min(5000, gap_ms))
    rest = not fire

    sequence = []
    for pin in pins:
        try:
            dev = _ensure_device(pin, initial_value=rest)
            dev.value = 1 if rest else 0
            dev.value = 1 if fire else 0
            time.sleep(duration_ms / 1000.0)
            dev.value = 1 if rest else 0
            sequence.append({"pin": pin, "ok": True})
        except Exception as e:
            sequence.append({"pin": pin, "ok": False, "error": str(e)})
        if gap_ms:
            time.sleep(gap_ms / 1000.0)
    return jsonify({
        "ok": True, "sequence": sequence,
        "duration_ms": duration_ms, "gap_ms": gap_ms,
        "fire_state": "high" if fire else "low",
    })
