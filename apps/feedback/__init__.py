"""
Feedback blueprint — local feedback storage tied to the page where it was filed.

Single-user / household use: no auth, no remote proxy. Items land in the
home-site SQLite DB and can be reviewed at /feedback/admin.

Each item gets a human-friendly ref_id (FB-0001…) for easy reference.
"""

from __future__ import annotations

import json

from flask import Blueprint, jsonify, render_template, request

bp = Blueprint("feedback", __name__, url_prefix="/feedback")

APP_META = {
    "slug": "feedback",
    "name": "Feedback",
    "tagline": "Page-tagged feedback inbox.",
    "icon": "✎",
    "url_endpoint": "feedback.admin_dashboard",
    "status_endpoint": None,
    "hidden": True,
}


VALID_PRIORITIES = {"low", "medium", "high", "urgent"}
VALID_STATUSES = {"new", "in_progress", "resolved", "dismissed"}


def _db():
    import app as _app
    return _app


def _ref_id(row_id: int) -> str:
    return f"FB-{row_id:04d}"


def _coerce_priority(value) -> str:
    p = (value or "medium").strip().lower()
    return p if p in VALID_PRIORITIES else "medium"


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------

@bp.route("/api/items", methods=["POST"])
def submit():
    data = request.get_json(silent=True) or {}
    items = data.get("items")
    if not isinstance(items, list) or not items:
        items = [data]

    created = []
    db = _db()
    with db.connect() as conn:
        for raw in items:
            title = (raw.get("title") or "").strip()
            if not title:
                continue
            page_path = (raw.get("page_path") or raw.get("page") or "/").strip() or "/"
            page_title = (raw.get("page_title") or "").strip() or None
            description = (raw.get("description") or "").strip() or None
            priority = _coerce_priority(raw.get("priority"))
            annotations = raw.get("annotations")
            ann_json = json.dumps(annotations) if annotations else None

            cur = conn.execute(
                """INSERT INTO feedback_items
                   (ref_id, page_path, page_title, title, description,
                    priority, annotations, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("__pending__", page_path, page_title, title, description,
                 priority, ann_json, db.now_iso(), db.now_iso()),
            )
            row_id = cur.lastrowid
            ref = _ref_id(row_id)
            conn.execute(
                "UPDATE feedback_items SET ref_id = ? WHERE id = ?",
                (ref, row_id),
            )
            created.append({"id": row_id, "ref_id": ref})

    if not created:
        return jsonify({"error": "title is required"}), 400
    return jsonify({"created": created, "count": len(created)}), 201


# ---------------------------------------------------------------------------
# List / detail
# ---------------------------------------------------------------------------

@bp.route("/api/items", methods=["GET"])
def list_items():
    page = request.args.get("page")
    status = request.args.get("status")
    priority = request.args.get("priority")

    clauses, params = [], []
    if page:
        clauses.append("page_path = ?")
        params.append(page)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if priority:
        clauses.append("priority = ?")
        params.append(priority)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM feedback_items{where} ORDER BY id DESC"

    db = _db()
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        if d.get("annotations"):
            try:
                d["annotations"] = json.loads(d["annotations"])
            except (TypeError, ValueError):
                d["annotations"] = []
        out.append(d)
    return jsonify(out)


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

@bp.route("/api/items/<int:item_id>", methods=["PATCH"])
def update_item(item_id):
    data = request.get_json(silent=True) or {}
    fields, params = [], []

    if "status" in data and data["status"] in VALID_STATUSES:
        fields.append("status = ?")
        params.append(data["status"])
    if "priority" in data and data["priority"] in VALID_PRIORITIES:
        fields.append("priority = ?")
        params.append(data["priority"])
    if not fields:
        return jsonify({"error": "nothing to update"}), 400

    db = _db()
    fields.append("updated_at = ?")
    params.append(db.now_iso())
    params.append(item_id)

    with db.connect() as conn:
        cur = conn.execute(
            f"UPDATE feedback_items SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        if cur.rowcount == 0:
            return jsonify({"error": "not found"}), 404

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

@bp.route("/api/items/<int:item_id>", methods=["DELETE"])
def delete_item(item_id):
    db = _db()
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM feedback_items WHERE id = ?", (item_id,))
        if cur.rowcount == 0:
            return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Admin dashboard
# ---------------------------------------------------------------------------

@bp.route("/admin")
def admin_dashboard():
    db = _db()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM feedback_items ORDER BY id DESC"
        ).fetchall()
        counts_rows = conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM feedback_items GROUP BY status"
        ).fetchall()

    items = []
    for r in rows:
        d = dict(r)
        if d.get("annotations"):
            try:
                d["annotations"] = json.loads(d["annotations"])
            except (TypeError, ValueError):
                d["annotations"] = []
        else:
            d["annotations"] = []
        items.append(d)

    counts = {r["status"]: r["cnt"] for r in counts_rows}
    return render_template(
        "feedback/admin.html",
        items=items,
        counts=counts,
        total=sum(counts.values()),
    )
