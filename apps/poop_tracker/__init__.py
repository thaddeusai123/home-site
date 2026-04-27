"""
Poop Tracker — log and analyze bowel movements for the kids.
Migrated from Home Lab to Home Website Pi.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify, render_template, request

bp = Blueprint("poop_tracker", __name__, url_prefix="/poop-tracker")

# Default cluster threshold — multiple poops within this window are treated as
# a single bowel "event" for gap and prediction analysis. Otherwise, several
# poops in a row in the same hour skew the avg gap toward minutes instead of
# the actual interval between bowel events. Override per-request with ?cluster=N.
DEFAULT_CLUSTER_HOURS = 2.0


def _cluster_times(times: list, threshold_hours: float) -> list[tuple]:
    """Group consecutive poops within `threshold_hours` of each other into
    clusters. Returns a list of (start, end, count) tuples — one per cluster,
    in chronological order."""
    if not times:
        return []
    threshold_secs = threshold_hours * 3600
    clusters = []
    cur_start = times[0]
    cur_end = times[0]
    cur_count = 1
    for t in times[1:]:
        if (t - cur_end).total_seconds() <= threshold_secs:
            cur_end = t
            cur_count += 1
        else:
            clusters.append((cur_start, cur_end, cur_count))
            cur_start = cur_end = t
            cur_count = 1
    clusters.append((cur_start, cur_end, cur_count))
    return clusters

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


def _log_poop(kid_id: int, occurred_at: str, notes: str = "") -> int:
    db = _db()
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO poop_log (kid_id, occurred_at, logged_at, notes) VALUES (?, ?, ?, ?)",
            (int(kid_id), occurred_at, db.now_iso(), notes),
        )
        poop_id = cur.lastrowid
        # Link any unlinked signs from the last 6 hours to this poop
        conn.execute(
            "UPDATE poop_signs SET poop_id = ? "
            "WHERE kid_id = ? AND poop_id IS NULL "
            "AND occurred_at >= datetime(?, '-6 hours')",
            (poop_id, kid_id, occurred_at),
        )
    return poop_id


def _log_sign(kid_id: int, occurred_at: str, notes: str = "") -> int:
    db = _db()
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO poop_signs (kid_id, sign_type, occurred_at, logged_at, notes) "
            "VALUES (?, 'sign', ?, ?, ?)",
            (int(kid_id), occurred_at, db.now_iso(), notes),
        )
    return cur.lastrowid


def _get_signs(kid_id: int, limit: int = 200) -> list[dict]:
    db = _db()
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM poop_signs WHERE kid_id = ? ORDER BY occurred_at DESC LIMIT ?",
            (kid_id, limit),
        ).fetchall()]


def _get_sign_stats(kid_id: int) -> dict:
    db = _db()
    with db.connect() as conn:
        signs = [dict(r) for r in conn.execute(
            "SELECT * FROM poop_signs WHERE kid_id = ? ORDER BY occurred_at ASC",
            (kid_id,),
        ).fetchall()]

    if not signs:
        return {
            "total": 0, "linked": 0, "unlinked": 0,
            "avg_sign_to_poop_mins": None,
            "sign_to_poop_times": [], "active_signs": [],
        }

    total = len(signs)
    linked = sum(1 for s in signs if s["poop_id"])
    unlinked = total - linked

    # Sign-to-poop delay for linked signs
    sign_to_poop_mins = []
    db2 = _db()
    with db2.connect() as conn:
        for s in signs:
            if not s["poop_id"]:
                continue
            poop = conn.execute(
                "SELECT occurred_at FROM poop_log WHERE id = ?", (s["poop_id"],)
            ).fetchone()
            if poop:
                try:
                    st = datetime.fromisoformat(s["occurred_at"])
                    pt = datetime.fromisoformat(poop["occurred_at"])
                    if st.tzinfo is None:
                        st = st.replace(tzinfo=timezone.utc)
                    if pt.tzinfo is None:
                        pt = pt.replace(tzinfo=timezone.utc)
                    delta = (pt - st).total_seconds() / 60
                    if delta >= 0:
                        sign_to_poop_mins.append(round(delta, 1))
                except Exception:
                    pass

    avg_delay = round(sum(sign_to_poop_mins) / len(sign_to_poop_mins), 1) if sign_to_poop_mins else None

    # Currently active (unlinked) signs in the last 6 hours
    now = datetime.now(timezone.utc)
    active = []
    for s in reversed(signs):
        if s["poop_id"]:
            continue
        try:
            st = datetime.fromisoformat(s["occurred_at"])
            if st.tzinfo is None:
                st = st.replace(tzinfo=timezone.utc)
            if (now - st).total_seconds() < 6 * 3600:
                mins_ago = round((now - st).total_seconds() / 60)
                active.append({"mins_ago": mins_ago, "at": s["occurred_at"]})
        except Exception:
            pass

    return {
        "total": total, "linked": linked, "unlinked": unlinked,
        "avg_sign_to_poop_mins": avg_delay,
        "sign_to_poop_times": sign_to_poop_mins[-20:],
        "active_signs": active,
    }


def _get_stats(kid_id: int, cluster_hours: float = DEFAULT_CLUSTER_HOURS) -> dict:
    db = _db()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT occurred_at FROM poop_log WHERE kid_id = ? ORDER BY occurred_at ASC",
            (kid_id,),
        ).fetchall()

    _empty = {"count": 0, "gaps": [], "avg_gap_hours": None, "last_at": None,
              "hours_since_last": None, "daily_counts": {}, "hourly_dist": [0]*24,
              "predicted_next": None, "warning": False, "weekly_avg": None,
              "cluster_count": 0, "avg_cluster_size": None,
              "cluster_threshold_hours": cluster_hours}

    if not rows:
        return {**_empty, "signs": _get_sign_stats(kid_id)}

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
        return {**_empty, "signs": _get_sign_stats(kid_id)}

    now = datetime.now(timezone.utc)

    # Cluster the poops — gap analysis runs on cluster boundaries, not individual poops.
    clusters = _cluster_times(times, cluster_hours)

    # Inter-cluster gaps: from the END of one cluster to the START of the next.
    # This reflects actual time between bowel events.
    gaps_hours = []
    for i in range(1, len(clusters)):
        gap = (clusters[i][0] - clusters[i-1][1]).total_seconds() / 3600
        gaps_hours.append(round(gap, 1))
    avg_gap = round(sum(gaps_hours) / len(gaps_hours), 1) if gaps_hours else None

    # "Last" is the end of the most recent cluster — that's when the body
    # finished its last bowel event.
    last_cluster_end = clusters[-1][1]
    hours_since = round((now - last_cluster_end).total_seconds() / 3600, 1)
    predicted_next = (last_cluster_end + timedelta(hours=avg_gap)).isoformat(timespec="minutes") if avg_gap else None
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

    avg_cluster_size = round(len(times) / len(clusters), 2) if clusters else None

    sign_stats = _get_sign_stats(kid_id)

    return {
        "count": len(times), "gaps": gaps_hours[-20:], "avg_gap_hours": avg_gap,
        "last_at": last_cluster_end.isoformat(timespec="minutes"),
        "hours_since_last": hours_since,
        "daily_counts": dict(sorted(daily_counts.items())[-30:]),
        "hourly_dist": hourly_dist, "predicted_next": predicted_next,
        "warning": warning, "weekly_avg": weekly_avg,
        "cluster_count": len(clusters),
        "avg_cluster_size": avg_cluster_size,
        "cluster_threshold_hours": cluster_hours,
        "signs": sign_stats,
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
    poop_id = _log_poop(int(kid_id), occurred_at, notes)
    return jsonify({"ok": True, "id": poop_id})


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
    cluster_raw = request.args.get("cluster")
    try:
        cluster_hours = float(cluster_raw) if cluster_raw else DEFAULT_CLUSTER_HOURS
    except ValueError:
        cluster_hours = DEFAULT_CLUSTER_HOURS
    if cluster_hours < 0:
        cluster_hours = 0.0
    return jsonify(_get_stats(kid_id, cluster_hours=cluster_hours))


@bp.route("/api/signs", methods=["POST"])
def api_log_sign():
    db = _db()
    data = request.get_json(force=True)
    kid_id = data.get("kid_id")
    if not kid_id:
        return jsonify({"error": "kid_id is required"}), 400
    occurred_at = data.get("occurred_at") or db.now_iso()
    notes = (data.get("notes") or "").strip()
    sign_id = _log_sign(int(kid_id), occurred_at, notes)
    return jsonify({"ok": True, "id": sign_id})


@bp.route("/api/signs/<int:sign_id>", methods=["DELETE"])
def api_delete_sign(sign_id):
    db = _db()
    with db.connect() as conn:
        conn.execute("DELETE FROM poop_signs WHERE id = ?", (sign_id,))
    return jsonify({"ok": True})


@bp.route("/api/signs/<int:kid_id>", methods=["GET"])
def api_get_signs(kid_id):
    db = _db()
    limit = request.args.get("limit", 200, type=int)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM poop_signs WHERE kid_id = ? ORDER BY occurred_at DESC LIMIT ?",
            (kid_id, limit),
        ).fetchall()
    return jsonify([dict(r) for r in rows])
