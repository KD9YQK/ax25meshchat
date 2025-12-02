# mesh_backend.py

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

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
    channel: str
    nick: str
    text: str
    timestamp: float
    origin_id: Optional[bytes] = None


@dataclass
class StatusEvent:
    text: str


UIEvent = ChatEvent | StatusEvent


# ============================================================
# Backend interface
# ============================================================

class BackendInterface:
    """
    Minimal interface expected by the GUI.
    """

    def send_message(self, channel: str, text: str) -> None:
        raise NotImplementedError

    def get_ui_queue(self) -> queue.Queue[UIEvent]:
        raise NotImplementedError

    def shutdown(self) -> None:
        raise NotImplementedError


# ============================================================
# MeshChatBackend
# ============================================================

class MeshChatBackend(BackendInterface):
    """
    Real backend wrapping MeshChatClient.

    - Runs MeshChatClient.start() in its own thread.
    - Bridges MeshChatClient callbacks into a UI event queue.
    - Exposes send_message() to the GUI.
    """

    def __init__(
            self,
            config: MeshChatConfig,
            default_peer_nick: str,
            status_heartbeat_interval: float = 0.0,
    ) -> None:
        """
        :param config: MeshChatConfig for MeshChatClient
        :param default_peer_nick: which peer to send messages to by default
        :param status_heartbeat_interval: if > 0, emits periodic StatusEvent heartbeats
        """
        self._config = config
        self._default_peer_nick = default_peer_nick
        self._ui_queue: queue.Queue[UIEvent] = queue.Queue()
        self._running = True

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

    # ----------------------------------------------------------
    # BackendInterface
    # ----------------------------------------------------------

    def get_ui_queue(self) -> queue.Queue[UIEvent]:
        return self._ui_queue

    def send_message(self, channel: str, text: str) -> None:
        """
        Called by the GUI when user hits Send.
        Uses a single default peer nick for now.
        """
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
        event = ChatEvent(
            channel=msg.channel,
            nick=msg.nick,
            text=msg.text,
            timestamp=ts,
            origin_id=origin_id,
        )
        self._ui_queue.put(event)

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
