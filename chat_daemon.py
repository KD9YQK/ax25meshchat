#!/usr/bin/env python3
"""Headless ARDOP mesh chat daemon.

This runs the existing MeshChatBackend without any GUI or user input.

Behavior:
- Participates fully as a mesh node (OGMs, forwarding, dedup, ordering)
- Receives/stores chat messages into the SQLite DB
- Responds to sync requests, performs gap detection + recovery
- Logs activity to stdout
- Runs until SIGINT/SIGTERM, then shuts down cleanly
"""

from __future__ import annotations

import argparse
import logging
import queue
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from config_loader import load_chat_config_from_yaml
from mesh_backend import (
    MeshChatBackend,
    ChatEvent,
    StatusEvent,
    NodeListEvent,
    ChannelListEvent,
    HistoryEvent,
)


def _configure_stdout_logging(verbosity: int) -> None:
    level = logging.INFO
    if verbosity >= 2:
        level = logging.DEBUG
    elif verbosity <= 0:
        level = logging.WARNING

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _resolve_db_path(config_path: Path, override: str) -> str:
    p = Path(override)
    if not p.is_absolute():
        p = config_path.parent.joinpath(p)
    return str(p.resolve())


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="ARDOP Mesh Chat headless daemon")
    ap.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    ap.add_argument(
        "--callsign",
        default="",
        help="Override mesh.callsign from config.yaml",
    )
    ap.add_argument(
        "--db-path",
        default="",
        help="Override chat.db_path from config.yaml (absolute or relative to config dir)",
    )
    ap.add_argument(
        "--mode",
        choices=["full", "relay", "monitor"],
        default="",
        help="Override chat.node_mode (full|relay|monitor)",
    )
    ap.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=1,
        help="Increase logging verbosity (use -vv for DEBUG)",
    )
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    _configure_stdout_logging(int(args.verbose))

    config_path = Path(str(args.config)).expanduser().resolve()
    chat_config = load_chat_config_from_yaml(str(config_path))

    mode_override = str(getattr(args, "mode", "") or "").strip().lower()
    if mode_override:
        chat_config.node_mode = mode_override

    # Optional overrides (additive, no schema changes)
    callsign = str(args.callsign or "").strip()
    if callsign:
        chat_config.mesh_node_config.callsign = callsign

    db_override = str(args.db_path or "").strip()
    if db_override:
        chat_config.db_path = _resolve_db_path(config_path, db_override)

    # Default peer nick is only required for GUI send semantics.
    # For a headless daemon (no user input), we allow peers to be empty.
    default_peer_nick = next(iter(chat_config.peers.keys()), "")

    backend = MeshChatBackend(
        config=chat_config,
        default_peer_nick=default_peer_nick,
        status_heartbeat_interval=60.0,
    )

    stop = False

    def _handle_signal(_signum: int, _frame) -> None:  # type: ignore[no-untyped-def]
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    ui_q = backend.get_ui_queue()

    # Main loop: drain backend UI queue and log to stdout.
    try:
        while not stop:
            try:
                ev = ui_q.get(timeout=0.5)
            except queue.Empty:
                continue

            if isinstance(ev, StatusEvent):
                print(f"[STATUS] {ev.text}")
            elif isinstance(ev, ChatEvent):
                ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ev.timestamp))
                print(f"[{ts_str}] {ev.channel} <{ev.nick}> {ev.text}")
            elif isinstance(ev, NodeListEvent):
                print(f"[NODES] {len(ev.nodes)}: {', '.join(ev.nodes)}")
            elif isinstance(ev, ChannelListEvent):
                # These are channel/DM identifiers excluding built-ins.
                print(f"[CHANNELS] {len(ev.channels)}: {', '.join(ev.channels)}")
            elif isinstance(ev, HistoryEvent):
                # History snapshots are primarily GUI-driven; log minimally.
                print(f"[HISTORY] {ev.channel}: {len(ev.messages)} message(s)")
            else:
                print(f"[EVENT] {ev!r}")

    finally:
        try:
            backend.shutdown()
        except (OSError, ValueError, AttributeError):
            pass

        # Give daemon threads a moment to wind down after stop() calls.
        time.sleep(0.2)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
