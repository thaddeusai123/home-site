"""
Poop Tracker — log and analyze bowel movements for the kids.
Migrated from Home Lab to Home Website Pi.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify, render_template, request

bp = Blueprint("poop_tracker", __name__, url_prefix="/poop-tracker")

APP_META = {
    "slug": "poop-tracker",
    "name": "Poop Tracker",
    "tagline": "Log and predict bowel movements.",
    "icon": "\U0001f4a9",
    "url_endpoint": "poop_tracker.index",
    "status_endpoint": None,
}


def _db():
    import app as _app
    return _app


def _ensure_default_kid():
    db = _db()
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM poop_kids WHERE name = ?", ("Penelope",)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO poop_kids (name, created_at) VALUES (?, ?)",
                ("Penelope", db.now_iso()),
            )


# Deferred init — called after app context is ready
_initialized = False


@bp.before_app_request
def _lazy_init():
    global _initialized
    if not _initialized:
        _ensure_default_kid()
        _initialized = True


def _list_kids():
    db = _db()
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM poop_kids ORDER BY id").fetchall()]


def _get_stats(kid_id: int) -> dict:
    db = _db()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT occurred_at FROM poop_log WHERE kid_id = ? ORDER BY occurred_at ASC",
            (kid_id,),
        ).fetchall()

    if not rows:
        return {"count": 0, "gaps": [], "avg_gap_hours": None, "last_at": None,
                "hours_since_last": None, "daily_counts": {}, "hourly_dist": [0]*24,
                "predicted_next": None, "warning": False, "weekly_avg": None}

    times = []
    for r in rows:
        try:
            t = datetime.fromisoformat(r["occurred_at"])
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            times.append(t)
        except Exception:
            pass

    if not times:
        return {"count": 0, "gaps": [], "avg_gap_hours": None, "last_at": None,
                "hours_since_last": None, "daily_counts": {}, "hourly_dist": [0]*24,
                "predicted_next": None, "warning": False, "weekly_avg": None}

    now = datetime.now(timezone.utc)
    gaps_hours = [round((times[i] - times[i-1]).total_seconds() / 3600, 1) for i in range(1, len(times))]
    avg_gap = round(sum(gaps_hours) / len(gaps_hours), 1) if gaps_hours else None
    last = times[-1]
    hours_since = round((now - last).total_seconds() / 3600, 1)
    predicted_next = (last + timedelta(hours=avg_gap)).isoformat(timespec="minutes") if avg_gap else None
    warning = bool(avg_gap and hours_since > avg_gap * 1.5)

    daily_counts = {}
    for t in times:
        day = t.strftime("%Y-%m-%d")
        daily_counts[day] = daily_counts.get(day, 0) + 1

    hourly_dist = [0] * 24
    for t in times:
        hourly_dist[t.hour] += 1

    if len(daily_counts) >= 2:
        days_span = (times[-1] - times[0]).days or 1
        weekly_avg = round(len(times) / (days_span / 7), 1) if days_span >= 1 else None
    else:
        weekly_avg = round(len(times) * 7, 1) if daily_counts else None

    return {
        "count": len(times), "gaps": gaps_hours[-20:], "avg_gap_hours": avg_gap,
        "last_at": last.isoformat(timespec="minutes"), "hours_since_last": hours_since,
        "daily_counts": dict(sorted(daily_counts.items())[-30:]),
        "hourly_dist": hourly_dist, "predicted_next": predicted_next,
        "warning": warning, "weekly_avg": weekly_avg,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    kids = _list_kids()
    return render_template("poop_tracker/index.html", kids=kids)


@bp.route("/api/kids", methods=["GET"])
def api_kids():
    return jsonify(_list_kids())


@bp.route("/api/kids", methods=["POST"])
def api_add_kid():
    db = _db()
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        with db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO poop_kids (name, created_at) VALUES (?, ?)",
                (name, db.now_iso()),
            )
            return jsonify({"ok": True, "id": cur.lastrowid, "name": name})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/api/kids/<int:kid_id>", methods=["DELETE"])
def api_delete_kid(kid_id):
    db = _db()
    with db.connect() as conn:
        conn.execute("DELETE FROM poop_kids WHERE id = ?", (kid_id,))
    return jsonify({"ok": True})


@bp.route("/api/log", methods=["POST"])
def api_log_poop():
    db = _db()
    data = request.get_json(force=True)
    kid_id = data.get("kid_id")
    if not kid_id:
        return jsonify({"error": "kid_id is required"}), 400
    occurred_at = data.get("occurred_at") or db.now_iso()
    notes = (data.get("notes") or "").strip()
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO poop_log (kid_id, occurred_at, logged_at, notes) VALUES (?, ?, ?, ?)",
            (int(kid_id), occurred_at, db.now_iso(), notes),
        )
    return jsonify({"ok": True, "id": cur.lastrowid})


@bp.route("/api/log/<int:poop_id>", methods=["DELETE"])
def api_delete_poop(poop_id):
    db = _db()
    with db.connect() as conn:
        conn.execute("DELETE FROM poop_log WHERE id = ?", (poop_id,))
    return jsonify({"ok": True})


@bp.route("/api/log/<int:kid_id>", methods=["GET"])
def api_get_log(kid_id):
    db = _db()
    limit = request.args.get("limit", 200, type=int)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM poop_log WHERE kid_id = ? ORDER BY occurred_at DESC LIMIT ?",
            (kid_id, limit),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route("/api/stats/<int:kid_id>", methods=["GET"])
def api_get_stats(kid_id):
    return jsonify(_get_stats(kid_id))
