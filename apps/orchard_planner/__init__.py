"""
Orchard Planner — interactive top-down apple orchard layout planner.
Migrated from Home Lab to Home Website Pi.
"""

from __future__ import annotations

import json

from flask import Blueprint, jsonify, render_template, request

bp = Blueprint("orchard_planner", __name__, url_prefix="/orchard-planner")

APP_META = {
    "slug": "orchard-planner",
    "name": "Orchard Planner",
    "tagline": "Interactive top-down apple orchard layout planner.",
    "icon": "🌳",
    "url_endpoint": "orchard_planner.planner_index",
    "status_endpoint": None,
}


def _db():
    import app as _app
    return _app


@bp.route("/")
def planner_index():
    return render_template("orchard_planner/planner.html")


@bp.route("/api/layouts", methods=["GET"])
def api_list_layouts():
    db = _db()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, name, updated_at FROM orchard_layouts ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return jsonify([dict(r) for r in rows])


@bp.route("/api/layouts", methods=["POST"])
def api_save_layout():
    db = _db()
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    data = body.get("data")
    if not name:
        return jsonify({"error": "name is required"}), 400
    if data is None:
        return jsonify({"error": "data is required"}), 400
    data_str = json.dumps(data) if not isinstance(data, str) else data

    with db.connect() as conn:
        existing = conn.execute(
            "SELECT id FROM orchard_layouts WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE orchard_layouts SET data = ?, updated_at = ? WHERE id = ?",
                (data_str, db.now_iso(), existing["id"]),
            )
            return jsonify({"ok": True, "id": existing["id"], "name": name})
        cur = conn.execute(
            "INSERT INTO orchard_layouts (name, data, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, data_str, db.now_iso(), db.now_iso()),
        )
        return jsonify({"ok": True, "id": cur.lastrowid, "name": name})


@bp.route("/api/layouts/<int:layout_id>", methods=["DELETE"])
def api_delete_layout(layout_id):
    db = _db()
    with db.connect() as conn:
        conn.execute("DELETE FROM orchard_layouts WHERE id = ?", (layout_id,))
    return jsonify({"ok": True})
