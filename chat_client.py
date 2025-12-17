# chat_client.py

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Tuple

from mesh_config import (
    MeshNodeConfig,
    ArdopConnectionConfig,
)
from ardop_link import ArdopLinkClient
from mesh_node import MeshNode
from chat_store import ChatStore
from chat_protocol import (
    ChatMessage,
    CHAT_TYPE_MESSAGE,
    CHAT_TYPE_SYNC_REQUEST,
    CHAT_TYPE_SYNC_RESPONSE,
    encode_chat_message,
    decode_chat_message,
    encode_sync_request,
    parse_sync_request,
    encode_sync_response,
    parse_sync_response,
)


@dataclass
class ChatPeer:
    node_id: bytes
    nick: str


@dataclass
class MeshChatConfig:
    mesh_node_config: MeshNodeConfig
    db_path: str
    peers: Dict[str, ChatPeer] = field(default_factory=dict)


class MeshChatClient:
    """
    IRC-style chat client on top of MeshNode with:

    - Persistent history via SQLite
    - Timestamped messages
    - Sync of chatrooms via SYNC_REQUEST / SYNC_RESPONSE
    """

    def __init__(
        self,
        config: MeshChatConfig,
        on_chat_message: Callable[[ChatMessage, bytes, float], None],
    ) -> None:
        """
        on_chat_message(ChatMessage, origin_id, ts)
        """
        self._config = config
        self._on_chat_message = on_chat_message
        self._nick = config.mesh_node_config.callsign  # default nick

        self._store = ChatStore(config.db_path)

        def link_client_factory(rx_callback):
            ardop_cfg = config.mesh_node_config.ardop_config
            if ardop_cfg is None:
                ardop_cfg = ArdopConnectionConfig()
            return ArdopLinkClient(ardop_cfg, rx_callback, name="mesh-ardop-link")

        self._mesh_node = MeshNode(
            config=config.mesh_node_config,
            link_client_factory=link_client_factory,
            app_data_callback=self._on_mesh_app_data,
        )

    # --------------------------------------------------------------
    # Lifecycle
    # --------------------------------------------------------------

    def start(self) -> None:
        self._mesh_node.start()

    def stop(self) -> None:
        self._mesh_node.stop()
        self._store.close()

    def set_nick(self, nick: str) -> None:
        self._nick = nick

    # --------------------------------------------------------------
    # Sending messages
    # --------------------------------------------------------------

    def send_message_to_peer(
        self,
        peer_nick: str,
        channel: str,
        text: str,
    ) -> None:
        peer = self._config.peers.get(peer_nick)
        if peer is None:
            raise ValueError(f"Unknown peer nickname: {peer_nick}")
        self.send_message_to_node(peer.node_id, channel, text)

    def send_message_to_node(
        self,
        dest_node_id: bytes,
        channel: str,
        text: str,
    ) -> None:
        msg = ChatMessage(
            msg_type=CHAT_TYPE_MESSAGE,
            channel=channel,
            nick=self._nick,
            text=text,
        )
        payload = encode_chat_message(msg)
        self._mesh_node.send_application_data(dest_node_id, payload)
        # Log locally as "sent"
        now = time.time()
        self._store.add_message(
            origin_id=self._mesh_node._node_id,  # you may want a public accessor
            seqno=0,  # we don't know data_seq here; optional local-only marker
            channel=channel,
            nick=self._nick,
            text=text,
            ts=now,
        )

    # --------------------------------------------------------------
    # Sync API
    # --------------------------------------------------------------

    def request_sync(
        self,
        dest_node_id: bytes,
        channel: str,
        since_ts: float,
    ) -> None:
        """
        Ask a peer for messages in `channel` after `since_ts`.
        """
        payload = encode_sync_request(channel=channel, nick=self._nick, since_ts=since_ts)
        self._mesh_node.send_application_data(dest_node_id, payload)

    def request_sync_from_peer(
        self,
        peer_nick: str,
        channel: str,
        since_ts: float,
    ) -> None:
        peer = self._config.peers.get(peer_nick)
        if peer is None:
            raise ValueError(f"Unknown peer nickname: {peer_nick}")
        self.request_sync(peer.node_id, channel, since_ts)

    def get_local_history(
        self,
        channel: str,
        limit: int = 100,
    ) -> List[Tuple[bytes, int, str, str, str, float]]:
        """
        Return local history for UI: list of
          (origin_id, seqno, channel, nick, text, ts)
        """
        return self._store.get_recent_messages(channel, limit)

    # --------------------------------------------------------------
    # Mesh app-data callback
    # --------------------------------------------------------------

    def _on_mesh_app_data(
        self,
        origin_id: bytes,
        _dest_id: bytes,
        data_seqno: int,
        payload: bytes,
    ) -> None:
        msg = decode_chat_message(payload)
        if msg is None:
            return

        now = time.time()

        if msg.msg_type == CHAT_TYPE_MESSAGE:
            self._handle_incoming_chat_message(origin_id, data_seqno, msg, now)
        elif msg.msg_type == CHAT_TYPE_SYNC_REQUEST:
            self._handle_sync_request(origin_id, data_seqno, msg)
        elif msg.msg_type == CHAT_TYPE_SYNC_RESPONSE:
            self._handle_sync_response(origin_id, data_seqno, msg, now)

    def _handle_incoming_chat_message(
        self,
        origin_id: bytes,
        data_seqno: int,
        msg: ChatMessage,
        ts: float,
    ) -> None:
        """
        Store and forward to UI.
        """
        self._store.add_message(
            origin_id=origin_id,
            seqno=data_seqno,
            channel=msg.channel,
            nick=msg.nick,
            text=msg.text,
            ts=ts,
        )
        self._on_chat_message(msg, origin_id, ts)

    def _handle_sync_request(
        self,
        origin_id: bytes,
        _data_seqno: int,
        msg: ChatMessage,
    ) -> None:
        since_ts = parse_sync_request(msg)
        if since_ts is None:
            return

        records_raw = self._store.get_messages_since(
            channel=msg.channel,
            since_ts=since_ts,
            limit=100,
        )

        records: List[Dict[str, object]] = []
        for origin_bytes, seqno, channel, nick, text, ts in records_raw:
            record = {
                "origin_id_hex": origin_bytes.hex(),
                "seqno": int(seqno),
                "nick": nick,
                "text": text,
                "ts": float(ts),
            }
            records.append(record)

        response_payload = encode_sync_response(
            channel=msg.channel,
            nick=self._nick,
            records=records,
        )
        # respond directly to requester
        self._mesh_node.send_application_data(origin_id, response_payload)

    def _handle_sync_response(
        self,
        _origin_id: bytes,
        _data_seqno: int,
        msg: ChatMessage,
        _now: float,
    ) -> None:
        records = parse_sync_response(msg)
        if records is None:
            return

        for record in records:
            origin_hex = record.get("origin_id_hex")
            seqno_val = record.get("seqno")
            nick_val = record.get("nick")
            text_val = record.get("text")
            ts_val = record.get("ts")

            if not isinstance(origin_hex, str):
                continue
            if not isinstance(seqno_val, int):
                continue
            if not isinstance(nick_val, str):
                continue
            if not isinstance(text_val, str):
                continue
            if not isinstance(ts_val, (float, int)):
                continue

            origin_bytes = bytes.fromhex(origin_hex)
            seqno_int = seqno_val
            ts_float = float(ts_val)

            if self._store.has_message(origin_bytes, seqno_int):
                continue

            self._store.add_message(
                origin_id=origin_bytes,
                seqno=seqno_int,
                channel=msg.channel,
                nick=nick_val,
                text=text_val,
                ts=ts_float,
            )

            chat_msg = ChatMessage(
                msg_type=CHAT_TYPE_MESSAGE,
                channel=msg.channel,
                nick=nick_val,
                text=text_val,
            )
            self._on_chat_message(chat_msg, origin_bytes, ts_float)
