# mesh_backend.py

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple

from chat_client import (
    MeshChatClient,
    MeshChatConfig,
    ChatMessage,
)


# ============================================================
# UI event dataclasses (shared with GUI)
# ============================================================

@dataclass
class ChatEvent:
    channel: str  # Display channel/tab name (e.g. "#general" or "@K0XYZ-7")
    nick: str
    text: str
    timestamp: float
    origin_id: Optional[bytes] = None


@dataclass
class StatusEvent:
    text: str


@dataclass
class NodeListEvent:
    # List of discovered node display names, e.g. ["K0ABC-7", "N0XYZ"]
    nodes: List[str]


@dataclass
class ChannelListEvent:
    # Local channels/DMs known from SQLite history (excluding built-ins)
    channels: List[str]


@dataclass
class HistoryEvent:
    channel: str
    # List of (origin_id, seqno, channel, nick, text, ts)
    messages: List[Tuple[bytes, int, str, str, str, float]]


UIEvent = ChatEvent | StatusEvent | NodeListEvent | ChannelListEvent | HistoryEvent


# ============================================================
# Backend interface
# ============================================================

class BackendInterface:
    """
    Minimal interface expected by the GUI.
    """

    def send_message(self, channel: str, text: str) -> None:
        """
        Send a message in the context of a given channel/tab.

        For example:
        - "#general" → normal channel message
        - "@K0XYZ-7" → direct message to peer K0XYZ-7
        """
        raise NotImplementedError

    def get_ui_queue(self) -> queue.Queue[UIEvent]:
        raise NotImplementedError

    def shutdown(self) -> None:
        raise NotImplementedError

    def request_history(self, channel: str, limit: int = 200) -> None:
        """Ask backend to emit HistoryEvent for a channel/DM."""
        raise NotImplementedError


# ============================================================
# MeshChatBackend
# ============================================================

class MeshChatBackend(BackendInterface):
    """
    Real backend wrapping MeshChatClient.

    - Runs MeshChatClient.start() in its own thread.
    - Bridges MeshChatClient callbacks into a UI event queue.
    - Exposes send_message() to the GUI, with DM and channel semantics.
    """

    def __init__(
            self,
            config: MeshChatConfig,
            default_peer_nick: str,
            status_heartbeat_interval: float = 0.0,
    ) -> None:
        """
        :param config: MeshChatConfig for MeshChatClient
        :param default_peer_nick: which peer to send *channel* messages to by default
        :param status_heartbeat_interval: if > 0, emits periodic StatusEvent heartbeats
        """
        self._config = config
        self._default_peer_nick = default_peer_nick
        self._ui_queue: queue.Queue[UIEvent] = queue.Queue()
        self._running = True
        self._last_nodes: List[str] = []
        self._last_channels: List[str] = []
        # Latest discovered mapping: callsign -> node_id
        self._discovered_node_ids: Dict[str, bytes] = {}
        # Sync throttling: (callsign, channel) -> last_sync_time (epoch seconds)
        self._last_sync_time: Dict[Tuple[str, str], float] = {}
        # Instantiate client with callback into UI queue
        self._client = MeshChatClient(
            config=config,
            on_chat_message=self._on_chat_message,
        )

        # Run MeshChatClient.start() in its own thread
        self._client_thread = threading.Thread(
            target=self._client.start,
            name="MeshChatClientMain",
            daemon=True,
        )
        self._client_thread.start()

        # Periodic node discovery snapshot -> UI events
        self._nodes_thread = threading.Thread(
            target=self._nodes_loop,
            name="MeshBackendNodes",
            daemon=True,
        )
        self._nodes_thread.start()

        # Optional status heartbeat
        self._status_interval = status_heartbeat_interval
        self._status_thread: Optional[threading.Thread] = None
        if self._status_interval > 0:
            self._status_thread = threading.Thread(
                target=self._status_loop,
                name="MeshBackendStatusHeartbeat",
                daemon=True,
            )
            self._status_thread.start()

        self._emit_status("MeshChat backend started.")

        # Initial channel list from SQLite so GUI can restore left list.
        self._emit_initial_channels()

    # ----------------------------------------------------------
    # BackendInterface
    # ----------------------------------------------------------

    def get_ui_queue(self) -> queue.Queue[UIEvent]:
        return self._ui_queue

    def send_message(self, channel: str, text: str) -> None:
        """
        Called by the GUI when user hits Send.

        Semantics:
        - If channel starts with '@', interpret as a direct message (DM)
          to that peer callsign/nickname.
        - Otherwise, treat it as a normal channel message sent to the
          default peer for now.
        """
        text = text.strip()
        if not text:
            return

        # DM: treat exactly like a channel, but route to a specific destination.
        # Convention: channel name is "@CALLSIGN".
        if channel.startswith("@") and len(channel) > 1:
            dest_callsign = channel[1:]

            # Prefer configured peers (by key), else fall back to discovered nodes.
            try:
                self._client.send_message_to_peer(
                    peer_nick=dest_callsign,
                    channel=channel,
                    text=text,
                )
            except ValueError:
                dest_node_id = self._discovered_node_ids.get(dest_callsign)
                if dest_node_id is None:
                    self._emit_status(f"Unknown DM destination: {dest_callsign}")
                    return
                try:
                    self._client.send_message_to_node(
                        dest_node_id=dest_node_id,
                        channel=channel,
                        text=text,
                    )
                except OSError as exc:
                    self._emit_status(f"DM transport error to {dest_callsign}: {exc}")

            self._refresh_channels_from_db()
            return

        # Normal channel message: use default peer for now
        try:
            self._client.send_message_to_peer(
                peer_nick=self._default_peer_nick,
                channel=channel,
                text=text,
            )
        except ValueError as exc:
            # Unknown peer nickname or similar configuration problem
            self._emit_status(f"Send error: {exc}")
        except OSError as exc:
            # Transport-level failures (serial/TCP issues, etc.)
            self._emit_status(f"Transport error: {exc}")

        self._refresh_channels_from_db()

    def shutdown(self) -> None:
        """
        Called by the GUI on application close.
        """
        self._running = False
        self._emit_status("Shutting down MeshChat backend...")
        self._client.stop()
        # No explicit join() needed for daemon threads,
        # but you can add it for debugging if you want:
        # self._client_thread.join(timeout=2.0)

    # ----------------------------------------------------------
    # MeshChatClient callback bridge
    # ----------------------------------------------------------

    def _on_chat_message(
            self,
            msg: ChatMessage,
            origin_id: bytes,
            ts: float,
    ) -> None:
        """
        Translate MeshChatClient messages into ChatEvents for the GUI.

        - Normal channels (e.g. "#general") are passed through as-is.
        - DMs are treated as just another channel (e.g. "@CALLSIGN").
        """
        event = ChatEvent(
            channel=msg.channel,
            nick=msg.nick,
            text=msg.text,
            timestamp=ts,
            origin_id=origin_id,
        )
        self._ui_queue.put(event)

        # Refresh local channel list as new channels/DMs appear.
        self._refresh_channels_from_db()

    # ----------------------------------------------------------
    # Status helpers
    # ----------------------------------------------------------

    def _emit_status(self, text: str) -> None:
        self._ui_queue.put(StatusEvent(text=text))

    def _status_loop(self) -> None:
        while self._running:
            time.sleep(self._status_interval)
            if not self._running:
                return
            self._emit_status("MeshChat backend heartbeat.")

    # ----------------------------------------------------------
    # Node/Channel state -> UI
    # ----------------------------------------------------------

    def _nodes_loop(self) -> None:
        """Periodically snapshot routing state and notify the GUI."""
        while self._running:
            time.sleep(1.0)
            if not self._running:
                return

            discovered = self._client.get_discovered_nodes()
            # Save mapping for DM fallback
            self._discovered_node_ids = {k: v[0] for k, v in discovered.items()}

            nodes = sorted(discovered.keys())
            if nodes != self._last_nodes:
                # detect newly seen callsigns
                old_set = set(self._last_nodes)
                new_set = set(nodes)
                newly_seen = sorted(new_set - old_set)

                self._last_nodes = nodes
                self._ui_queue.put(NodeListEvent(nodes=nodes))

                # Auto-sync on new peer (if enabled)
                self._maybe_auto_sync_new_peers(newly_seen)

    def _maybe_auto_sync_new_peers(self, newly_seen: List[str]) -> None:
        cfg = self._config

        # Respect config
        if not getattr(cfg, "sync_enabled", True):
            return
        if not getattr(cfg, "sync_auto_sync_on_new_peer", True):
            return
        if not newly_seen:
            return

        last_n = int(getattr(cfg, "sync_last_n_messages", 200))
        if last_n <= 0:
            return

        min_interval = float(getattr(cfg, "sync_min_sync_interval_seconds", 30.0))
        if min_interval < 0.0:
            min_interval = 0.0

        # Keep it minimal and safe: auto-sync only normal channels (not DMs)
        # Start with #general plus any other known local channels that begin with '#'.
        # Build channel list from DB at sync time (do not rely on cached GUI state)
        try:
            local_channels = self._client.get_local_channels()
        except (OSError, ValueError):
            local_channels = []

        channels_to_sync: List[str] = ["#general"]
        for c in local_channels:
            if isinstance(c, str) and c.startswith("#") and c not in channels_to_sync:
                channels_to_sync.append(c)

        now = time.time()

        for callsign in newly_seen:
            node_id = self._discovered_node_ids.get(callsign)
            if node_id is None:
                continue

            for channel in channels_to_sync:
                key = (callsign, channel)
                last_ts = self._last_sync_time.get(key)
                if last_ts is not None and (now - last_ts) < min_interval:
                    continue

                try:
                    self._client.request_sync_last_n(
                        dest_node_id=node_id,
                        channel=channel,
                        last_n=last_n,
                    )
                    self._last_sync_time[key] = now
                except (OSError, ValueError):
                    # No broad exceptions; just skip this peer/channel this tick.
                    continue

    def request_history(self, channel: str, limit: int = 200) -> None:
        """Emit a HistoryEvent for `channel` based on local SQLite history.

        This does not create any fake messages; it is a replay of persisted state.
        The GUI decides when to request (typically when opening a tab).
        """
        try:
            msgs = self._client.get_local_history(channel, limit=limit)
        except (OSError, ValueError):
            return
        self._ui_queue.put(HistoryEvent(channel=channel, messages=msgs))

    def _emit_initial_channels(self) -> None:
        """Send ChannelListEvent based on SQLite so GUI can restore left list."""
        try:
            channels = [c for c in self._client.get_local_channels() if c != "#general"]
        except (OSError, ValueError):
            channels = []

        self._last_channels = sorted(channels)
        self._ui_queue.put(ChannelListEvent(channels=self._last_channels))

    def _refresh_channels_from_db(self) -> None:
        """Refresh GUI-visible channel list from SQLite when it changes."""
        try:
            channels = [c for c in self._client.get_local_channels() if c != "#general"]
        except (OSError, ValueError):
            return

        new_list = sorted(channels)
        if new_list != self._last_channels:
            self._last_channels = new_list
            self._ui_queue.put(ChannelListEvent(channels=new_list))
