"""
Feedback blueprint — local feedback storage tied to the page where it was filed.

Single-user / household use: no auth, no remote proxy. Items land in the
home-site SQLite DB and can be reviewed at /feedback/admin.

Each item gets a human-friendly ref_id (FB-0001…) for easy reference.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

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
# Bulk update
# ---------------------------------------------------------------------------

@bp.route("/api/items/bulk", methods=["POST"])
def bulk_update():
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or []
    status = data.get("status")
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "ids required"}), 400
    if status not in VALID_STATUSES:
        return jsonify({"error": "valid status required"}), 400

    db = _db()
    placeholders = ",".join("?" for _ in ids)
    with db.connect() as conn:
        cur = conn.execute(
            f"UPDATE feedback_items SET status = ?, updated_at = ? "
            f"WHERE id IN ({placeholders})",
            [status, db.now_iso()] + list(ids),
        )
    return jsonify({"ok": True, "updated": cur.rowcount})


# ---------------------------------------------------------------------------
# Export — page-grouped JSON for batch processing
# ---------------------------------------------------------------------------

@bp.route("/api/items/export", methods=["GET"])
def export_items():
    status_filter = request.args.get("status", "new,in_progress")
    statuses = [s.strip() for s in status_filter.split(",") if s.strip()]
    if not statuses:
        return jsonify({"error": "status filter required"}), 400

    placeholders = ",".join("?" for _ in statuses)
    sql = (
        f"SELECT * FROM feedback_items WHERE status IN ({placeholders}) "
        "ORDER BY page_path, id"
    )
    db = _db()
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, statuses).fetchall()]

    pages_map: dict[str, dict] = {}
    for r in rows:
        path = r["page_path"]
        bucket = pages_map.setdefault(path, {
            "page_path": path,
            "page_title": r.get("page_title"),
            "items": [],
        })
        if r.get("page_title"):
            bucket["page_title"] = r["page_title"]
        try:
            anns = json.loads(r["annotations"]) if r.get("annotations") else []
        except (TypeError, ValueError):
            anns = []
        bucket["items"].append({
            "ref_id": r["ref_id"],
            "title": r["title"],
            "description": r.get("description"),
            "priority": r["priority"],
            "status": r["status"],
            "created_at": r["created_at"],
            "annotations": anns,
        })

    pages = list(pages_map.values())
    total = sum(len(p["items"]) for p in pages)
    return jsonify({
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_items": total,
        "pages": pages,
    })


# ---------------------------------------------------------------------------
# Admin dashboard
# ---------------------------------------------------------------------------

@bp.route("/admin")
def admin_dashboard():
    db = _db()
    with db.connect() as conn:
        counts_rows = conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM feedback_items GROUP BY status"
        ).fetchall()
        page_rows = conn.execute(
            "SELECT DISTINCT page_path FROM feedback_items ORDER BY page_path"
        ).fetchall()

    counts = {r["status"]: r["cnt"] for r in counts_rows}
    pages = [r["page_path"] for r in page_rows]
    return render_template(
        "feedback/admin.html",
        pages=pages,
        counts=counts,
        total=sum(counts.values()),
    )
