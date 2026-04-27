"""
GENA event subscription + in-process pub/sub for SSE streaming.

For each discovered speaker we subscribe to its AVTransport,
RenderingControl, and ZoneGroupTopology events. SoCo runs its own HTTP
listener and dispatches NOTIFY callbacks into per-subscription queues. A
worker thread drains those queues and re-publishes normalized events to
every connected SSE client.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any

from soco.events import event_listener
from soco.exceptions import SoCoException

from . import sonos_client


_subs_lock = threading.RLock()
_subs: dict[str, list] = {}              # speaker_uid -> [Subscription, ...]
_drain_threads: dict[str, threading.Thread] = {}

_clients_lock = threading.RLock()
_clients: list[queue.Queue] = []          # one queue per connected SSE client

_started = False
_start_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Pub / sub fanout
# ---------------------------------------------------------------------------

def _publish(event_type: str, uid: str, payload: dict) -> None:
    msg = json.dumps({"type": event_type, "uid": uid, "payload": payload})
    with _clients_lock:
        for q in list(_clients):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


def subscribe_client() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=200)
    with _clients_lock:
        _clients.append(q)
    return q


def unsubscribe_client(q: queue.Queue) -> None:
    with _clients_lock:
        try:
            _clients.remove(q)
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# GENA subscription per speaker
# ---------------------------------------------------------------------------

_SERVICES = ("avTransport", "renderingControl", "zoneGroupTopology")


def _subscribe_speaker(sp) -> None:
    """Idempotently subscribe to all event services for one speaker."""
    with _subs_lock:
        if sp.uid in _subs:
            return
        subs = []
        for service_attr in _SERVICES:
            service = getattr(sp, service_attr)
            try:
                sub = service.subscribe(auto_renew=True, requested_timeout=600)
                subs.append(sub)
            except SoCoException:
                continue
        _subs[sp.uid] = subs

        t = threading.Thread(
            target=_drain_loop, args=(sp.uid, subs), daemon=True,
            name=f"sonos-events-{sp.player_name}",
        )
        _drain_threads[sp.uid] = t
        t.start()


def _drain_loop(uid: str, subs: list) -> None:
    """Pull events from all subscription queues for one speaker, fan out."""
    while True:
        any_event = False
        for sub in subs:
            try:
                ev = sub.events.get(timeout=0.5)
            except queue.Empty:
                continue
            except Exception:
                continue
            any_event = True
            _handle_event(uid, ev)
        if not any_event:
            # Bail out if all subs are dead (e.g., speaker offline)
            with _subs_lock:
                if uid not in _subs:
                    return
            time.sleep(0.05)


def _handle_event(uid: str, ev) -> None:
    """Translate a SoCo Event into a published delta the UI can consume."""
    vars_ = ev.variables or {}
    service = ev.service.service_type if ev.service else "?"
    if service == "AVTransport":
        try:
            sp = sonos_client.get(uid)
            snap = sonos_client.state_snapshot(sp)
        except Exception:
            snap = {"raw": {k: str(v)[:200] for k, v in vars_.items()}}
        _publish("transport", uid, snap)
    elif service == "RenderingControl":
        try:
            sp = sonos_client.get(uid)
            payload = {"volume": sp.volume, "mute": sp.mute}
        except Exception:
            payload = {}
        _publish("volume", uid, payload)
    elif service == "ZoneGroupTopology":
        try:
            speakers = [sonos_client.speaker_summary(s) for s in sonos_client.all_speakers()]
        except Exception:
            speakers = []
        _publish("topology", uid, {"speakers": speakers})


def start() -> None:
    """Discover speakers and start GENA subscriptions. Idempotent."""
    global _started
    with _start_lock:
        if _started:
            return
        speakers = sonos_client.discover()
        for sp in speakers:
            try:
                _subscribe_speaker(sp)
            except Exception:
                continue
        _started = True


def shutdown() -> None:
    with _subs_lock:
        for uid, subs in list(_subs.items()):
            for sub in subs:
                try:
                    sub.unsubscribe()
                except Exception:
                    pass
            _subs.pop(uid, None)
    try:
        event_listener.stop()
    except Exception:
        pass
