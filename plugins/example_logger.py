# ./plugins/logger.py
# Minimal example plugin for Feature #7 (local-only).
# This plugin writes selected events to a local log file.
#
# Supported styles:
#  - Define functions named exactly like the event: on_message_received(event), etc.
#  - Or define handle_event(name, event) to receive everything.
#
# This file is intentionally tiny and safe.

from __future__ import annotations

import json
import time
from typing import Any, Dict

_LOG_PATH = "./plugin_events.log"


def _safe_log(obj: Dict[str, Any]) -> None:
    try:
        line = json.dumps(obj, sort_keys=True, ensure_ascii=False)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        # Never let plugin logging break the mesh.
        return


def handle_event(name: str, event: Dict[str, Any]) -> None:
    """
    Catch-all handler: receives every event.
    """
    # Keep it small: log the event name and a shallow preview of data keys.
    data = event.get("data", {})
    preview = {}
    if isinstance(data, dict):
        # Only keep a few useful keys if present
        for k in ("channel", "sender", "origin", "seqno", "peer_id", "link_id", "state", "reason"):
            if k in data:
                preview[k] = data[k]

    _safe_log(
        {
            "ts": event.get("ts", time.time()),
            "name": name,
            "preview": preview,
        }
    )


# Optional: you can also implement specific handlers. If your loader calls both,
# keep these light (or remove them and rely only on handle_event).
def on_message_received(event: Dict[str, Any]) -> None:
    handle_event("on_message_received", event)


def on_message_sent(event: Dict[str, Any]) -> None:
    handle_event("on_message_sent", event)


def on_message_stored(event: Dict[str, Any]) -> None:
    handle_event("on_message_stored", event)


def on_gap_detected(event: Dict[str, Any]) -> None:
    handle_event("on_gap_detected", event)


def on_sync_applied(event: Dict[str, Any]) -> None:
    handle_event("on_sync_applied", event)


def on_prune_executed(event: Dict[str, Any]) -> None:
    handle_event("on_prune_executed", event)


def on_link_state_change(event: Dict[str, Any]) -> None:
    handle_event("on_link_state_change", event)
