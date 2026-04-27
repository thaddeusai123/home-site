"""
SoCo wrapper: discovery, connection cache, and thin helpers around the
operations the blueprint needs. Everything here is synchronous and
thread-safe (Waitress runs each request in its own thread).
"""
from __future__ import annotations

import threading
from typing import Any

import soco
from soco.exceptions import SoCoException


_lock = threading.RLock()
_speakers: dict[str, soco.SoCo] = {}   # uid -> SoCo
_last_discovery: float = 0.0


def _persist_cache() -> None:
    """Write current cache to sonos_speakers table for cross-restart hints."""
    import app as _app
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _app.connect() as conn:
        for uid, sp in _speakers.items():
            try:
                info = sp.get_speaker_info(refresh=False)
                conn.execute(
                    "INSERT INTO sonos_speakers (uid, room_name, model, ip_address, last_seen) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(uid) DO UPDATE SET "
                    "room_name=excluded.room_name, model=excluded.model, "
                    "ip_address=excluded.ip_address, last_seen=excluded.last_seen",
                    (uid, info.get("zone_name") or "Unknown",
                     info.get("model_name") or "",
                     sp.ip_address, now),
                )
            except SoCoException:
                continue


def discover(force: bool = False, timeout: int = 5) -> list[soco.SoCo]:
    """SSDP-discover all ZonePlayers on the LAN, populate cache."""
    global _last_discovery
    import time
    with _lock:
        if not force and _speakers and (time.time() - _last_discovery) < 60:
            return list(_speakers.values())
        found = soco.discover(timeout=timeout, allow_network_scan=True) or set()
        _speakers.clear()
        for sp in found:
            _speakers[sp.uid] = sp
        _last_discovery = time.time()
        _persist_cache()
        return list(_speakers.values())


def all_speakers(block: bool = True) -> list[soco.SoCo]:
    """Return cached speakers. If `block=False`, never trigger an SSDP scan
    (used by the SSE handler so it doesn't stall for ~5s on a cold cache)."""
    with _lock:
        if _speakers:
            return list(_speakers.values())
    if not block:
        return []
    return discover()


def get(uid: str) -> soco.SoCo:
    with _lock:
        sp = _speakers.get(uid)
        if sp is None:
            discover()
            sp = _speakers.get(uid)
        if sp is None:
            raise KeyError(f"speaker {uid} not found")
        return sp


# ---------------------------------------------------------------------------
# Snapshots — pure-data dicts the SSE / REST layer can serialize.
# ---------------------------------------------------------------------------

def speaker_summary(sp: soco.SoCo) -> dict[str, Any]:
    """Cheap-ish summary used in the speaker list. Tolerates errors."""
    try:
        info = sp.get_speaker_info(refresh=False)
    except SoCoException:
        info = {}
    try:
        group = sp.group
        coord_uid = group.coordinator.uid if group and group.coordinator else sp.uid
        member_uids = [m.uid for m in (group.members if group else [])]
        group_label = group.label if group else sp.player_name
    except SoCoException:
        coord_uid, member_uids, group_label = sp.uid, [sp.uid], sp.player_name

    return {
        "uid": sp.uid,
        "name": sp.player_name,
        "model": info.get("model_name") or "",
        "ip": sp.ip_address,
        "is_coordinator": coord_uid == sp.uid,
        "coordinator_uid": coord_uid,
        "group_members": member_uids,
        "group_label": group_label,
    }


def state_snapshot(sp: soco.SoCo) -> dict[str, Any]:
    """Full per-speaker state snapshot for the now-playing / control panel."""
    coord = sp.group.coordinator if sp.group else sp
    try:
        ti = coord.get_current_transport_info()
    except SoCoException:
        ti = {}
    try:
        track = coord.get_current_track_info()
    except SoCoException:
        track = {}
    try:
        vol = sp.volume
        mute = sp.mute
    except SoCoException:
        vol, mute = 0, False

    # Sonos returns "NOT_IMPLEMENTED" for duration on streams without a
    # known length (radio, some yt-dlp inputs). Normalize to empty string
    # so the UI can detect unknown duration cleanly.
    duration = track.get("duration") or ""
    if duration == "NOT_IMPLEMENTED":
        duration = ""

    return {
        "uid": sp.uid,
        "coordinator_uid": coord.uid,
        "transport_state": ti.get("current_transport_state", "STOPPED"),
        "play_mode": (ti.get("current_play_mode") or "NORMAL"),
        "volume": vol,
        "mute": mute,
        "track": {
            "title": track.get("title") or "",
            "artist": track.get("artist") or "",
            "album": track.get("album") or "",
            "album_art": _abs_art(sp, track.get("album_art")),
            "position": track.get("position") or "0:00:00",
            "duration": duration,
            "uri": track.get("uri") or "",
            "playlist_position": int(track.get("playlist_position") or 0),
        },
    }


def _abs_art(sp: soco.SoCo, art: str | None) -> str | None:
    if not art:
        return None
    if art.startswith("http://") or art.startswith("https://"):
        return art
    return f"http://{sp.ip_address}:1400{art}"


# ---------------------------------------------------------------------------
# Transport / volume / queue wrappers
# ---------------------------------------------------------------------------

def transport(sp: soco.SoCo, action: str, **kw) -> None:
    coord = sp.group.coordinator if sp.group else sp
    if action == "play":
        # Sonos returns UPnP 701 "transition not available" if Play is
        # invoked with no current AVTransport URI. When the queue has
        # items, fall through to play_from_queue(0); otherwise let the
        # caller surface the error.
        try:
            coord.play()
        except Exception:
            try:
                if coord.queue_size > 0:
                    coord.play_from_queue(0)
                    return
            except Exception:
                pass
            raise
    elif action == "pause":
        coord.pause()
    elif action == "stop":
        coord.stop()
    elif action == "next":
        coord.next()
    elif action == "previous":
        coord.previous()
    elif action == "seek":
        position = kw.get("position") or "0:00:00"
        coord.seek(position)
    else:
        raise ValueError(f"unknown transport action: {action}")


def queue_list(sp: soco.SoCo) -> list[dict]:
    coord = sp.group.coordinator if sp.group else sp
    items = coord.get_queue(start=0, max_items=500, full_album_art_uri=True)
    out = []
    for i, t in enumerate(items):
        out.append({
            "index": i,
            "title": getattr(t, "title", "") or "",
            "creator": getattr(t, "creator", "") or "",
            "album": getattr(t, "album", "") or "",
            "uri": getattr(t, "resources", [None])[0].uri if getattr(t, "resources", None) else "",
            "album_art": getattr(t, "album_art_uri", "") or "",
        })
    return out


def queue_add(sp: soco.SoCo, uri: str, metadata: str = "", as_next: bool = False) -> int:
    coord = sp.group.coordinator if sp.group else sp
    return coord.add_uri_to_queue(uri, as_next=as_next)


def queue_remove(sp: soco.SoCo, index: int) -> None:
    coord = sp.group.coordinator if sp.group else sp
    coord.remove_from_queue(index)


def queue_clear(sp: soco.SoCo) -> None:
    coord = sp.group.coordinator if sp.group else sp
    coord.clear_queue()


def queue_reorder(sp: soco.SoCo, from_idx: int, to_idx: int) -> None:
    """ReorderTracksInQueue uses 1-based indexes; InsertBefore is the
    target slot the track should land in front of."""
    coord = sp.group.coordinator if sp.group else sp
    coord.avTransport.ReorderTracksInQueue([
        ("InstanceID", 0),
        ("StartingIndex", from_idx + 1),
        ("NumberOfTracks", 1),
        ("InsertBefore", to_idx + 1),
        ("UpdateID", 0),
    ])


def queue_save(sp: soco.SoCo, name: str) -> None:
    coord = sp.group.coordinator if sp.group else sp
    coord.create_sonos_playlist_from_queue(name)


# ---------------------------------------------------------------------------
# Browse: favorites, sonos playlists, music services
# ---------------------------------------------------------------------------

def _favorites(sp: soco.SoCo) -> list:
    return list(sp.music_library.get_sonos_favorites(max_items=500))


def _playlists(sp: soco.SoCo) -> list:
    return list(sp.get_sonos_playlists(max_items=500))


def browse(sp: soco.SoCo, root: str) -> dict:
    """root is one of: favorites, playlists, services."""
    if root == "favorites":
        return {"root": root, "items": [_didl_to_dict(t, i) for i, t in enumerate(_favorites(sp))]}
    if root == "playlists":
        return {"root": root, "items": [_didl_to_dict(t, i) for i, t in enumerate(_playlists(sp))]}
    if root == "services":
        from soco.music_services import MusicService
        try:
            names = MusicService.get_all_music_services_names() or []
        except Exception:
            names = []
        return {"root": root, "items": [{"idx": i, "title": n} for i, n in enumerate(names)]}
    return {"root": root, "items": []}


def _didl_to_dict(t, idx: int) -> dict:
    res = getattr(t, "resources", None) or []
    uri = res[0].uri if res else ""
    return {
        "idx": idx,
        "title": getattr(t, "title", "") or "",
        "creator": getattr(t, "creator", "") or "",
        "album": getattr(t, "album", "") or "",
        "uri": uri,
        "album_art": getattr(t, "album_art_uri", "") or "",
    }


def play_favorite(sp: soco.SoCo, idx: int) -> None:
    coord = sp.group.coordinator if sp.group else sp
    favs = _favorites(sp)
    if idx < 0 or idx >= len(favs):
        raise KeyError(f"favorite index out of range: {idx}")
    f = favs[idx]
    res = f.resources[0]
    # Favorites can be either tracks (queueable) or radio/streams (play directly).
    proto = (res.protocol_info or "").lower()
    if "x-rincon" in res.uri or "x-sonosapi-radio" in res.uri or "rtp" in proto:
        coord.play_uri(res.uri, meta="", start=True)
    else:
        try:
            coord.clear_queue()
            coord.add_to_queue(f)
            coord.play_from_queue(0)
        except Exception:
            coord.play_uri(res.uri, meta="", start=True)


def play_sonos_playlist(sp: soco.SoCo, idx: int) -> None:
    coord = sp.group.coordinator if sp.group else sp
    pls = _playlists(sp)
    if idx < 0 or idx >= len(pls):
        raise KeyError(f"playlist index out of range: {idx}")
    coord.clear_queue()
    coord.add_to_queue(pls[idx])
    coord.play_from_queue(0)


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------

def set_volume(sp: soco.SoCo, volume: int | None = None, mute: bool | None = None) -> None:
    if volume is not None:
        sp.volume = max(0, min(100, int(volume)))
    if mute is not None:
        sp.mute = bool(mute)


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def set_group(coordinator_uid: str, member_uids: list[str]) -> None:
    """Make `member_uids` exactly the membership of coordinator's group."""
    coord = get(coordinator_uid)
    desired = set(member_uids) | {coordinator_uid}
    current = {m.uid for m in (coord.group.members if coord.group else [coord])}

    for uid in desired - current:
        sp = get(uid)
        sp.join(coord)
    for uid in current - desired:
        if uid == coordinator_uid:
            continue
        sp = get(uid)
        sp.unjoin()


# ---------------------------------------------------------------------------
# Direct URL playback (used by the yt-dlp path)
# ---------------------------------------------------------------------------

def play_uri(sp: soco.SoCo, uri: str, meta_xml: str = "", title: str = "") -> None:
    coord = sp.group.coordinator if sp.group else sp
    coord.play_uri(uri, meta=meta_xml, title=title or "Stream", start=True)
