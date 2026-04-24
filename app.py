"""
Home Website — personal tools and apps, served from a Raspberry Pi on the
local network. Nginx reverse-proxies port 80/443 to this Waitress instance.

Architecture mirrors the Home Lab: Flask blueprints registered in APPS,
landing page at /, sidebar nav auto-populated from APP_META.
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

# ---------------------------------------------------------------------------
# App registry
# ---------------------------------------------------------------------------

APPS = [
    "apps.orchard_planner",
    "apps.poop_tracker",
    "apps.nutrient_calc",
    "apps.gpio_lab",
]

app = Flask(__name__)
app.secret_key = os.environ.get("HOMESITE_SECRET_KEY", "homesite-dev-key")

APP_NAME = "Home Website"
APP_PORT = int(os.environ.get("HOMESITE_PORT", 8080))

# ---------------------------------------------------------------------------
# Embedded SQLite (self-contained — no dependency on the Home Lab's db.py)
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get(
    "HOMESITE_DB_PATH",
    os.path.join(os.path.dirname(__file__), "homesite.db"),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS orchard_layouts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                data        TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS poop_kids (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                created_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS poop_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                kid_id      INTEGER NOT NULL REFERENCES poop_kids(id) ON DELETE CASCADE,
                occurred_at TEXT NOT NULL,
                logged_at   TEXT NOT NULL,
                notes       TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS poop_signs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                kid_id      INTEGER NOT NULL REFERENCES poop_kids(id) ON DELETE CASCADE,
                sign_type   TEXT NOT NULL DEFAULT 'sign',
                occurred_at TEXT NOT NULL,
                logged_at   TEXT NOT NULL,
                poop_id     INTEGER REFERENCES poop_log(id) ON DELETE SET NULL,
                notes       TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_poop_log_kid ON poop_log(kid_id);
            CREATE INDEX IF NOT EXISTS idx_poop_log_occurred ON poop_log(occurred_at);
            CREATE INDEX IF NOT EXISTS idx_poop_signs_kid ON poop_signs(kid_id);
            CREATE INDEX IF NOT EXISTS idx_poop_signs_occurred ON poop_signs(occurred_at);
            CREATE TABLE IF NOT EXISTS nutrient_recipes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                data        TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gpio_labels (
                pin         INTEGER PRIMARY KEY,
                label       TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
        """)


init_db()

# ---------------------------------------------------------------------------
# Blueprint registration
# ---------------------------------------------------------------------------

_registered_apps: list[dict] = []

for module_path in APPS:
    mod = importlib.import_module(module_path)
    bp = getattr(mod, "bp", None)
    meta = getattr(mod, "APP_META", None)
    if bp is None or meta is None:
        raise RuntimeError(f"{module_path} must export `bp` and `APP_META`")
    app.register_blueprint(bp)
    _registered_apps.append(meta)


# ---------------------------------------------------------------------------
# Template globals
# ---------------------------------------------------------------------------

@app.context_processor
def inject_identity():
    slug = None
    if request.blueprint:
        for meta in _registered_apps:
            if meta["slug"].replace("-", "_") == request.blueprint:
                slug = meta["slug"]
                break
    return {
        "app_name": APP_NAME,
        "app_port": APP_PORT,
        "now_year": datetime.now(timezone.utc).year,
        "registered_apps": _registered_apps,
        "current_app_slug": slug,
    }


# ---------------------------------------------------------------------------
# Core routes
# ---------------------------------------------------------------------------

@app.route("/")
def landing():
    return render_template("landing.html", apps=_registered_apps)


@app.route("/healthz")
def healthz():
    return jsonify({
        "ok": True,
        "app": APP_NAME,
        "port": APP_PORT,
        "registered_apps": [a["slug"] for a in _registered_apps],
    })


@app.route("/api/system/restart", methods=["POST"])
def api_system_restart():
    """Restart the Home Website service via systemctl."""
    import subprocess
    confirm = (request.form.get("confirm") or "").strip().lower()
    if confirm not in {"1", "true", "yes", "on", "confirm"}:
        return jsonify({"error": "confirmation required: pass confirm=true"}), 400
    try:
        subprocess.Popen(
            ["sudo", "systemctl", "restart", "home-site"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        return jsonify({"error": f"restart failed: {e}"}), 500
    return jsonify({"ok": True, "detail": "restart scheduled"})


@app.errorhandler(404)
def not_found(_e):
    return render_template("base.html", not_found=True), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=APP_PORT, debug=False)
