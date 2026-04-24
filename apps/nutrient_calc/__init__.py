"""
Nutrient Calculator — Masterblend 4-18-38 + Calcium Nitrate + Epsom
mixing calculator with live NPK ppm readout and a pH-down estimator.
Math is client-side; saved recipes persist in SQLite.
"""

from __future__ import annotations

import json

from flask import Blueprint, jsonify, render_template, request

bp = Blueprint("nutrient_calc", __name__, url_prefix="/nutrient-calc")

APP_META = {
    "slug": "nutrient-calc",
    "name": "Nutrient Calculator",
    "tagline": "Masterblend NPK mixer + pH down estimator.",
    "icon": "\U0001f9ea",
    "url_endpoint": "nutrient_calc.index",
    "status_endpoint": None,
}


def _db():
    import app as _app
    return _app


@bp.route("/")
def index():
    return render_template("nutrient_calc/index.html")


@bp.route("/api/recipes", methods=["GET"])
def api_list_recipes():
    db = _db()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, name, data, updated_at FROM nutrient_recipes ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return jsonify([
        {"id": r["id"], "name": r["name"], "data": json.loads(r["data"]), "updated_at": r["updated_at"]}
        for r in rows
    ])


@bp.route("/api/recipes", methods=["POST"])
def api_save_recipe():
    db = _db()
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    data = body.get("data")
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not isinstance(data, dict):
        return jsonify({"error": "data must be an object"}), 400
    data_str = json.dumps(data)
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT id FROM nutrient_recipes WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE nutrient_recipes SET data = ?, updated_at = ? WHERE id = ?",
                (data_str, db.now_iso(), existing["id"]),
            )
            return jsonify({"ok": True, "id": existing["id"], "name": name, "updated": True})
        cur = conn.execute(
            "INSERT INTO nutrient_recipes (name, data, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, data_str, db.now_iso(), db.now_iso()),
        )
        return jsonify({"ok": True, "id": cur.lastrowid, "name": name, "updated": False})


@bp.route("/api/recipes/<int:rid>", methods=["DELETE"])
def api_delete_recipe(rid):
    db = _db()
    with db.connect() as conn:
        conn.execute("DELETE FROM nutrient_recipes WHERE id = ?", (rid,))
    return jsonify({"ok": True})
