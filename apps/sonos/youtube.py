"""
YouTube Music fallback path.

- ytmusicapi for search (no auth needed for public catalog).
- yt-dlp to extract a direct audio URL Sonos can play via SetAVTransportURI.

Both libraries are heavy to import; do it lazily so app startup stays fast.
"""
from __future__ import annotations

import html
from typing import Any

_yt = None  # ytmusicapi.YTMusic singleton


def _ytmusic():
    global _yt
    if _yt is None:
        from ytmusicapi import YTMusic
        _yt = YTMusic()
    return _yt


def search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    yt = _ytmusic()
    raw = yt.search(query, filter="songs", limit=limit) or []
    out = []
    for r in raw:
        vid = r.get("videoId")
        if not vid:
            continue
        artists = ", ".join(a.get("name", "") for a in r.get("artists") or [])
        thumbs = r.get("thumbnails") or []
        art = thumbs[-1]["url"] if thumbs else ""
        out.append({
            "video_id": vid,
            "title": r.get("title") or "",
            "artist": artists,
            "album": (r.get("album") or {}).get("name", "") if r.get("album") else "",
            "duration": r.get("duration") or "",
            "thumbnail": art,
            "url": f"https://music.youtube.com/watch?v={vid}",
        })
    return out


def extract_audio(url: str) -> dict[str, Any]:
    """Return {'stream_url', 'title', 'artist', 'duration', 'thumbnail'}."""
    from yt_dlp import YoutubeDL
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "stream_url": info.get("url"),
        "title": info.get("track") or info.get("title") or "",
        "artist": info.get("artist") or info.get("uploader") or "",
        "album": info.get("album") or "",
        "duration": info.get("duration") or 0,
        "thumbnail": info.get("thumbnail") or "",
    }


def didl_metadata(stream_url: str, title: str, artist: str = "",
                  album: str = "", art: str = "") -> str:
    """Build a minimal DIDL-Lite XML so the speaker shows track info."""
    e = lambda s: html.escape(s or "", quote=True)
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/">'
        '<item id="ytdlp" parentID="-1" restricted="true">'
        f'<dc:title>{e(title)}</dc:title>'
        f'<dc:creator>{e(artist)}</dc:creator>'
        f'<upnp:album>{e(album)}</upnp:album>'
        f'<upnp:albumArtURI>{e(art)}</upnp:albumArtURI>'
        '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        '<res protocolInfo="http-get:*:audio/mpeg:*">'
        f'{e(stream_url)}'
        '</res>'
        '</item>'
        '</DIDL-Lite>'
    )
