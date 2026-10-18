"""Server-clock activity accounting; persist the returned dictionary in context.

No client timestamp or legacy wall-clock elapsed value is accepted as evidence.
Reads use a projection; commands persist the advanced state in their transaction.
"""
from __future__ import annotations

import copy
import math
from datetime import datetime, timezone, timedelta, time

from .rolling import calendar_zone

ACTIVITY_VERSION = "server-active-v1"
HEARTBEAT_SECONDS = 30
LIVENESS_SECONDS = 90
_EVENTS = {
    "pause": ("manual_pause", True), "resume": ("manual_pause", False),
    "hidden": ("hidden", True), "visible": ("hidden", False),
    "disconnect": ("disconnected", True), "reconnect": ("disconnected", False),
    "assessment_start": ("assessment", True), "assessment_end": ("assessment", False),
    "waiting": ("waiting", True), "ready": ("waiting", False),
}


def _timestamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.timestamp()
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("server_now must be finite")
    return value


def _append_segment(segments, start, end):
    if end <= start:
        return
    if segments and segments[-1]['end'] == start:
        segments[-1]['end'] = end
    else:
        segments.append({'start': start, 'end': end})


def _daily_seconds(segments, calendar_timezone):
    zone = timezone.utc if calendar_timezone in {'UTC', 'Etc/UTC'} else calendar_zone(calendar_timezone)
    result = {}
    for segment in segments:
        start, end = segment['start'], segment['end']
        while start < end:
            local = datetime.fromtimestamp(start, timezone.utc).astimezone(zone)
            midnight = datetime.combine(local.date() + timedelta(days=1), time(), tzinfo=zone).timestamp()
            stop = min(end, midnight)
            key = local.date().isoformat()
            result[key] = result.get(key, 0.0) + stop - start
            start = stop
    return result


def activity_daily_seconds(activity, calendar_timezone='Asia/Shanghai'):
    """Rebucket confirmed intervals in any current budget calendar timezone.

    Older aggregate seconds without interval evidence are intentionally not
    assigned to a fabricated date. Reads may pass an activity_snapshot first.
    """
    return _daily_seconds((activity or {}).get('active_segments', []), calendar_timezone)


def new_activity(server_now, calendar_timezone='Asia/Shanghai'):
    timestamp = _timestamp(server_now)
    if calendar_timezone not in {'UTC', 'Etc/UTC'}:
        calendar_zone(calendar_timezone)
    return {"version": ACTIVITY_VERSION, "active_seconds": 0.0,
            "paused_seconds": 0.0, "active_segments": [], "paused_segments": [],
            "calendar_timezone": calendar_timezone, "daily_active_seconds": {}, "daily_paused_seconds": {},
            "last_accounted_at": timestamp, "last_heartbeat_at": timestamp,
            "pause_reasons": [], "status": "active", "reliable": True,
            "quality_flags": [],
            "heartbeat_seconds": HEARTBEAT_SECONDS, "liveness_seconds": LIVENESS_SECONDS}


def activity_snapshot(activity, server_now):
    """Project current activity without modifying stored state or renewing liveness."""
    timestamp = _timestamp(server_now)
    if not activity or activity.get("version") != ACTIVITY_VERSION:
        state = new_activity(timestamp)
        state["reliable"] = False
        return state
    state = copy.deepcopy(activity)
    state.setdefault('active_segments', [])
    state.setdefault('paused_segments', [])
    state.setdefault('paused_seconds', 0.0)
    state.setdefault('calendar_timezone', 'Asia/Shanghai')
    state.setdefault('quality_flags', [])
    timestamp = max(timestamp, state["last_accounted_at"])
    if state["status"] != "active":
        return state
    cutoff = state["last_heartbeat_at"] + LIVENESS_SECONDS
    start = state['last_accounted_at']
    if not state["pause_reasons"]:
        active_end = max(start, min(timestamp, cutoff))
        state["active_seconds"] += active_end - start
        _append_segment(state['active_segments'], start, active_end)
        paused_start = active_end
    else:
        paused_start = start
    state['paused_seconds'] += timestamp - paused_start
    _append_segment(state['paused_segments'], paused_start, timestamp)
    state['daily_active_seconds'] = activity_daily_seconds(state, state['calendar_timezone'])
    state['daily_paused_seconds'] = _daily_seconds(state['paused_segments'], state['calendar_timezone'])
    state["last_accounted_at"] = timestamp
    if timestamp > cutoff:
        if not state['pause_reasons']:
            state['quality_flags'] = sorted(set(state['quality_flags']) | {'heartbeat_timeout'})
        state["pause_reasons"] = sorted(set(state["pause_reasons"]) | {"disconnected"})
    return state


def advance_activity(activity, server_now, event="heartbeat"):
    """Settle the previous interval, then apply an event at server receipt time.

    A heartbeat reconnects transport only. Visibility and manual pause are changed
    by their own events so refresh and automatic recovery preserve user intent.
    """
    if event not in {*_EVENTS, "heartbeat", "finish"}:
        raise ValueError(f"Unknown activity event: {event}")
    state = activity_snapshot(activity, server_now)
    if state["status"] != "active":
        return state
    timestamp = max(_timestamp(server_now), state["last_accounted_at"])
    reasons = set(state["pause_reasons"])
    if event in _EVENTS:
        reason, add = _EVENTS[event]
        reasons.add(reason) if add else reasons.discard(reason)
    if event in {"heartbeat", "reconnect", "visible", "resume", "ready"}:
        reasons.discard("disconnected")
    if event == "finish":
        state["status"] = "finished"
    state["last_heartbeat_at"] = timestamp
    state["pause_reasons"] = sorted(reasons)
    return state
