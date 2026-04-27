"""
Sonos Control — local-LAN replacement for the Sonos S2 mobile app.

Talks to ZonePlayers over UPnP SOAP via SoCo. Real-time updates flow from
GENA event subscriptions through an in-process pub/sub to a Server-Sent
Events stream. YouTube Music has two paths: the official Sonos service
(once the user has linked it in S2) and a yt-dlp fallback for arbitrary
YT/YT Music URLs.
"""
from __future__ import annotations

import json
import queue
import time

from flask import Blueprint, Response, jsonify, render_template, request, stream_with_context

from . import events, sonos_client, youtube

bp = Blueprint("sonos", __name__, url_prefix="/sonos")

APP_META = {
    "slug": "sonos",
    "name": "Sonos Control",
    "tagline": "Local-LAN control for Sonos speakers — transport, queue, grouping, YouTube Music.",
    "icon": "\U0001f50a",
    "url_endpoint": "sonos.index",
    "status_endpoint": None,
}


_initialized = False


@bp.before_app_request
def _lazy_start():
    global _initialized
    if _initialized:
        return
    _initialized = True
    try:
        events.start()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    return render_template("sonos/index.html")


# ---------------------------------------------------------------------------
# Speakers + state
# ---------------------------------------------------------------------------

@bp.route("/api/speakers")
def api_speakers():
    speakers = sonos_client.all_speakers()
    return jsonify([sonos_client.speaker_summary(s) for s in speakers])


@bp.route("/api/speakers/refresh", methods=["POST"])
def api_refresh():
    speakers = sonos_client.discover(force=True)
    return jsonify([sonos_client.speaker_summary(s) for s in speakers])


@bp.route("/api/state/<uid>")
def api_state(uid):
    try:
        sp = sonos_client.get(uid)
    except KeyError:
        return jsonify({"error": "speaker not found"}), 404
    return jsonify(sonos_client.state_snapshot(sp))


# ---------------------------------------------------------------------------
# Transport / volume
# ---------------------------------------------------------------------------

@bp.route("/api/transport/<uid>/<action>", methods=["POST"])
def api_transport(uid, action):
    try:
        sp = sonos_client.get(uid)
    except KeyError:
        return jsonify({"error": "speaker not found"}), 404
    body = request.get_json(silent=True) or {}
    try:
        sonos_client.transport(sp, action, **body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@bp.route("/api/volume/<uid>", methods=["POST"])
def api_volume(uid):
    try:
        sp = sonos_client.get(uid)
    except KeyError:
        return jsonify({"error": "speaker not found"}), 404
    body = request.get_json(silent=True) or {}
    sonos_client.set_volume(sp, volume=body.get("volume"), mute=body.get("mute"))
    return jsonify({"ok": True, "volume": sp.volume, "mute": sp.mute})


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

@bp.route("/api/queue/<uid>")
def api_queue(uid):
    try:
        sp = sonos_client.get(uid)
    except KeyError:
        return jsonify({"error": "speaker not found"}), 404
    return jsonify(sonos_client.queue_list(sp))


@bp.route("/api/queue/<uid>/add", methods=["POST"])
def api_queue_add(uid):
    try:
        sp = sonos_client.get(uid)
    except KeyError:
        return jsonify({"error": "speaker not found"}), 404
    body = request.get_json(force=True)
    uri = body.get("uri")
    if not uri:
        return jsonify({"error": "uri is required"}), 400
    pos = sonos_client.queue_add(sp, uri, body.get("metadata") or "",
                                 as_next=body.get("position") == "next")
    return jsonify({"ok": True, "position": pos})


@bp.route("/api/queue/<uid>/remove", methods=["POST"])
def api_queue_remove(uid):
    try:
        sp = sonos_client.get(uid)
    except KeyError:
        return jsonify({"error": "speaker not found"}), 404
    body = request.get_json(force=True)
    if body.get("clear"):
        sonos_client.queue_clear(sp)
    else:
        sonos_client.queue_remove(sp, int(body["index"]))
    return jsonify({"ok": True})


@bp.route("/api/queue/<uid>/reorder", methods=["POST"])
def api_queue_reorder(uid):
    try:
        sp = sonos_client.get(uid)
    except KeyError:
        return jsonify({"error": "speaker not found"}), 404
    body = request.get_json(force=True)
    sonos_client.queue_reorder(sp, int(body["from"]), int(body["to"]))
    return jsonify({"ok": True})


@bp.route("/api/queue/<uid>/save", methods=["POST"])
def api_queue_save(uid):
    try:
        sp = sonos_client.get(uid)
    except KeyError:
        return jsonify({"error": "speaker not found"}), 404
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    sonos_client.queue_save(sp, name)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Browse / play favorites + playlists
# ---------------------------------------------------------------------------

@bp.route("/api/browse")
def api_browse():
    root = request.args.get("root", "favorites")
    speakers = sonos_client.all_speakers()
    if not speakers:
        return jsonify({"error": "no speakers"}), 404
    return jsonify(sonos_client.browse(speakers[0], root))


@bp.route("/api/play/favorite", methods=["POST"])
def api_play_favorite():
    body = request.get_json(force=True)
    sp = sonos_client.get(body["uid"])
    sonos_client.play_favorite(sp, int(body["idx"]))
    return jsonify({"ok": True})


@bp.route("/api/play/playlist", methods=["POST"])
def api_play_playlist():
    body = request.get_json(force=True)
    sp = sonos_client.get(body["uid"])
    sonos_client.play_sonos_playlist(sp, int(body["idx"]))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

@bp.route("/api/group", methods=["POST"])
def api_group():
    body = request.get_json(force=True)
    sonos_client.set_group(body["coordinator_uid"], body.get("member_uids") or [])
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# YouTube Music (yt-dlp + ytmusicapi)
# ---------------------------------------------------------------------------

@bp.route("/api/ytdlp/search")
def api_ytdlp_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    try:
        return jsonify(youtube.search(q, limit=20))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/ytdlp/play", methods=["POST"])
def api_ytdlp_play():
    body = request.get_json(force=True)
    uid = body.get("uid")
    url = body.get("url")
    if not uid or not url:
        return jsonify({"error": "uid and url are required"}), 400
    try:
        sp = sonos_client.get(uid)
    except KeyError:
        return jsonify({"error": "speaker not found"}), 404
    try:
        info = youtube.extract_audio(url)
    except Exception as e:
        return jsonify({"error": f"yt-dlp failed: {e}"}), 502
    if not info.get("stream_url"):
        return jsonify({"error": "no audio stream extracted"}), 502
    meta = youtube.didl_metadata(
        info["stream_url"], info["title"], info["artist"],
        info["album"], info["thumbnail"],
    )
    sonos_client.play_uri(sp, info["stream_url"], meta_xml=meta, title=info["title"])
    return jsonify({"ok": True, "title": info["title"], "artist": info["artist"]})


# ---------------------------------------------------------------------------
# SSE event stream
# ---------------------------------------------------------------------------

@bp.route("/api/events/stream")
def api_events_stream():
    @stream_with_context
    def gen():
        q = events.subscribe_client()
        # initial snapshot so the client doesn't have to wait for the first event
        try:
            speakers = [sonos_client.speaker_summary(s)
                        for s in sonos_client.all_speakers()]
            yield "data: " + json.dumps({"type": "topology", "uid": "",
                                         "payload": {"speakers": speakers}}) + "\n\n"
        except Exception:
            pass
        try:
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"   # keepalive
        except GeneratorExit:
            pass
        finally:
            events.unsubscribe_client(q)

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(gen(), headers=headers)
