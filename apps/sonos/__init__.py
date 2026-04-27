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

from . import events, proxy, sonos_client, youtube

bp = Blueprint("sonos", __name__, url_prefix="/sonos")

APP_META = {
    "slug": "sonos",
    "name": "Sonos Control",
    "tagline": "Local-LAN control for Sonos speakers — transport, queue, grouping, YouTube Music.",
    "icon": "\U0001f50a",
    "url_endpoint": "sonos.index",
    "status_endpoint": None,
}


import threading as _threading

_initialized = False
_init_lock = _threading.Lock()


def _bg_start():
    try:
        events.start()
    except Exception:
        pass


@bp.before_app_request
def _lazy_start():
    """Kick off discovery + GENA subscriptions on first request, in a
    background thread so the request itself returns immediately. The first
    SSE handler previously blocked ~5s on SSDP, which the browser
    EventSource interpreted as a failed connection and reconnected forever."""
    global _initialized
    with _init_lock:
        if _initialized:
            return
        _initialized = True
    _threading.Thread(target=_bg_start, daemon=True, name="sonos-bg-start").start()


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
        # Surface UPnP error codes (e.g. 701 "transition not available"
        # when the queue is empty) instead of a generic 500.
        msg = str(e)
        code = getattr(e, "error_code", None)
        if code == "701" or "701" in msg:
            return jsonify({
                "error": "Nothing to play — queue is empty. Pick something from Browse first.",
                "upnp_code": "701",
            }), 409
        return jsonify({"error": msg}), 500
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
    import os
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

    # Wrap the upstream URL in a local LAN proxy so Sonos sees a clean
    # http://<pi-lan-ip>:<port>/sonos/stream/<token> URL with a sane
    # Content-Type instead of a signed googlevideo HTTPS URL (which it
    # often refuses with UPnP 701).
    token = proxy.register(info["stream_url"], content_type="audio/mp4")
    pi_ip = proxy.lan_ip_for(sp.ip_address)
    port = int(os.environ.get("HOMESITE_PORT", 8080))
    proxied = f"http://{pi_ip}:{port}/sonos/stream/{token}"

    meta = youtube.didl_metadata(
        proxied, info["title"], info["artist"], info["album"], info["thumbnail"],
    )
    try:
        sonos_client.play_uri(sp, proxied, meta_xml=meta, title=info["title"])
    except Exception as e:
        return jsonify({"error": f"speaker rejected stream: {e}"}), 502
    return jsonify({"ok": True, "title": info["title"], "artist": info["artist"]})


# ---------------------------------------------------------------------------
# Stream proxy (Sonos hits this; not for browsers)
# ---------------------------------------------------------------------------

@bp.route("/stream/<token>")
def stream_proxy(token):
    range_header = request.headers.get("Range")
    gen, headers, status, _ = proxy.stream_response(token, range_header)
    if gen is None:
        return Response("not found", status=404)
    return Response(gen(), status=status, headers=headers, direct_passthrough=True)


# ---------------------------------------------------------------------------
# SSE event stream
# ---------------------------------------------------------------------------

@bp.route("/api/events/stream")
def api_events_stream():
    # Subscribe BEFORE flushing the first byte so any events fired during
    # the initial topology snapshot don't get lost.
    q = events.subscribe_client()

    def gen():
        # Bytes (not str) because direct_passthrough=True bypasses Flask's
        # automatic str→bytes encoding.
        yield b"retry: 3000\n\n"
        try:
            speakers = sonos_client.all_speakers(block=False)
            payload = {"speakers": [sonos_client.speaker_summary(s) for s in speakers]}
            yield ("data: " + json.dumps({"type": "topology", "uid": "",
                                          "payload": payload}) + "\n\n").encode()
        except Exception:
            pass
        try:
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield (f"data: {msg}\n\n").encode()
                except queue.Empty:
                    yield b": ping\n\n"   # keepalive comment
        except GeneratorExit:
            pass
        finally:
            events.unsubscribe_client(q)

    # NOTE: must NOT set "Connection" header — PEP 3333 forbids hop-by-hop
    # headers from a WSGI app and Waitress raises AssertionError otherwise.
    headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }
    return Response(stream_with_context(gen()), headers=headers,
                    direct_passthrough=True)
