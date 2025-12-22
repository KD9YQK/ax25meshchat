from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Tuple, Optional

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
    encode_sync_request_seqno,
    encode_sync_request_range,
    parse_sync_request_any,
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

    # Sync configuration (all optional; defaults are sane)
    sync_enabled: bool = True
    sync_last_n_messages: int = 200
    sync_max_send_per_response: int = 200
    sync_auto_sync_on_new_peer: bool = True
    sync_min_sync_interval_seconds: float = 30.0

    # Gap-related sync policy (v2, policy layer)
    gap_related_sync_enabled: bool = True
    gap_related_min_interval_seconds: float = 120.0
    gap_related_jitter_seconds: float = 2.0
    # If >0, override sync_last_n_messages for gap-related last-N requests (policy layer)
    gap_related_last_n_messages: int = 0

    # Targeted sync (range) policy tuning
    targeted_sync_enabled: bool = True
    # Merge ranges if they are overlapping, adjacent, or within this many seqnos.
    targeted_sync_merge_distance: int = 0
    # Maximum inclusive length of any single requested range (end-start+1).
    targeted_sync_max_range_len: int = 50
    # Maximum number of range requests to emit per confirmed-gap trigger.
    targeted_sync_max_requests_per_trigger: int = 3


# --------------------------------------------------------------
# Gap detection (local, in-memory)
# --------------------------------------------------------------

class _GapTracker:
    """Track missing (origin_id, seqno) under out-of-order delivery.

    This is purely observational (no protocol changes, no auto-sync triggers).
    It distinguishes:
      - suspected gaps: recently detected, may still be in-flight
      - confirmed gaps: persisted beyond a local time threshold
    """

    def __init__(
            self,
            confirm_delay_seconds: float = 90.0,
            min_report_interval_seconds: float = 15.0,
    ) -> None:
        self._confirm_delay = float(confirm_delay_seconds)
        if self._confirm_delay < 0.0:
            self._confirm_delay = 0.0

        self._min_report_interval = float(min_report_interval_seconds)
        if self._min_report_interval < 0.0:
            self._min_report_interval = 0.0

        # origin_hex -> state
        self._origins: Dict[str, Dict[str, object]] = {}

    @staticmethod
    def _origin_label(origin_id: bytes) -> str:
        callsign = origin_id.rstrip(b"\x00").decode("ascii", errors="ignore")
        if callsign:
            return callsign
        return origin_id.hex()

    @staticmethod
    def _add_range(
            ranges: List[Tuple[int, int, float]],
            start: int,
            end: int,
            detected_ts: float,
    ) -> List[Tuple[int, int, float]]:
        if start > end:
            return ranges

        new_ranges: List[Tuple[int, int, float]] = []
        inserted = False
        s = int(start)
        e = int(end)
        ts0 = float(detected_ts)

        for rs, re_, rts in ranges:
            if re_ + 1 < s:
                new_ranges.append((rs, re_, rts))
                continue
            if e + 1 < rs:
                if not inserted:
                    new_ranges.append((s, e, ts0))
                    inserted = True
                new_ranges.append((rs, re_, rts))
                continue

            # overlap/adjacent: merge
            s = min(s, rs)
            e = max(e, re_)
            ts0 = min(ts0, float(rts))

        if not inserted:
            new_ranges.append((s, e, ts0))

        new_ranges.sort(key=lambda t: t[0])
        return new_ranges

    @staticmethod
    def _remove_seq(
            ranges: List[Tuple[int, int, float]],
            seqno: int,
    ) -> List[Tuple[int, int, float]]:
        x = int(seqno)
        out: List[Tuple[int, int, float]] = []
        for rs, re_, rts in ranges:
            if x < rs or x > re_:
                out.append((rs, re_, rts))
                continue
            # split
            if rs <= x - 1:
                out.append((rs, x - 1, rts))
            if x + 1 <= re_:
                out.append((x + 1, re_, rts))
        return out

    @staticmethod
    def _ranges_signature(ranges: List[Tuple[int, int, float]], confirmed: List[bool]) -> str:
        parts: List[str] = []
        for (rs, re_, _), is_conf in zip(ranges, confirmed):
            parts.append(f"{rs}-{re_}:{'C' if is_conf else 'S'}")
        return "|".join(parts)

    def on_seqno(self, origin_id: bytes, seqno: int, now: float) -> List[str]:
        """Ingest a received seqno. Returns any new report lines to emit."""
        if seqno < 0:
            return []

        origin_hex = origin_id.hex()
        st = self._origins.get(origin_hex)
        if st is None:
            st = {
                "hi_contig": -1,
                "ooo": {},  # seqno -> None
                "missing": [],  # List[(start,end,detected_ts)]
                "last_report_ts": 0.0,
                "last_sig": "",
            }
            self._origins[origin_hex] = st

        hi_contig = int(st["hi_contig"])  # type: ignore[assignment]
        ooo: Dict[int, None] = st["ooo"]  # type: ignore[assignment]
        missing: List[Tuple[int, int, float]] = st["missing"]  # type: ignore[assignment]

        # Dedup purely in-memory: if we've already seen it, ignore.
        if seqno <= hi_contig or seqno in ooo:
            return []

        if seqno == hi_contig + 1:
            hi_contig = seqno
            # Advance through any buffered out-of-order messages
            while (hi_contig + 1) in ooo:
                del ooo[hi_contig + 1]
                hi_contig += 1
        else:
            # Gap discovered: everything between (hi_contig+1 .. seqno-1)
            gap_start = hi_contig + 1
            gap_end = seqno - 1
            missing = self._add_range(missing, gap_start, gap_end, now)
            # Buffer this out-of-order seq
            ooo[seqno] = None
            # If this seq was previously considered missing, clear it
            missing = self._remove_seq(missing, seqno)

        # Also clear this seq from missing (in case it was already in a range)
        missing = self._remove_seq(missing, seqno)

        # If contig advanced, drop any missing ranges that are now below hi_contig
        if missing:
            trimmed: List[Tuple[int, int, float]] = []
            for rs, re_, rts in missing:
                if re_ <= hi_contig:
                    continue
                if rs <= hi_contig:
                    rs = hi_contig + 1
                trimmed.append((rs, re_, rts))
            missing = trimmed

        st["hi_contig"] = hi_contig
        st["ooo"] = ooo
        st["missing"] = missing

        return self._maybe_report(origin_id, st, now)

    def _maybe_report(self, origin_id: bytes, st: Dict[str, object], now: float) -> List[str]:
        missing: List[Tuple[int, int, float]] = st["missing"]  # type: ignore[assignment]
        if not missing:
            # If we previously reported gaps, allow a one-time "resolved" message.
            last_sig = str(st.get("last_sig") or "")
            if last_sig:
                last_ts = float(st.get("last_report_ts") or 0.0)
                if (now - last_ts) >= self._min_report_interval:
                    st["last_report_ts"] = now
                    st["last_sig"] = ""
                    label = self._origin_label(origin_id)
                    return [f"{label} gaps resolved"]
            return []

        confirmed_flags: List[bool] = []
        for rs, re_, detected_ts in missing:
            confirmed_flags.append((now - float(detected_ts)) >= self._confirm_delay)

        sig = self._ranges_signature(missing, confirmed_flags)
        last_sig = str(st.get("last_sig") or "")
        last_ts = float(st.get("last_report_ts") or 0.0)

        if sig == last_sig:
            return []

        if (now - last_ts) < self._min_report_interval:
            return []

        st["last_report_ts"] = now
        st["last_sig"] = sig

        label = self._origin_label(origin_id)

        # Emit one line per status bucket to reduce spam.
        lines: List[str] = []
        for want_confirmed in (False, True):
            bucket: List[Tuple[int, int]] = []
            for (rs, re_, _), is_conf in zip(missing, confirmed_flags):
                if is_conf == want_confirmed:
                    bucket.append((rs, re_))
            if not bucket:
                continue
            ranges_str = ", ".join([f"{a}" if a == b else f"{a}\u2013{b}" for a, b in bucket])
            state = "confirmed" if want_confirmed else "suspected"
            lines.append(f"{label} missing seq {ranges_str} ({state})")
        return lines


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
            on_sync_applied: Optional[Callable[[str, int], None]] = None,
            on_gap_report: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        on_chat_message(ChatMessage, origin_id, created_ts)
        """
        self._config = config
        self._on_chat_message = on_chat_message
        self._on_sync_applied = on_sync_applied
        self._on_gap_report = on_gap_report
        self._gap_tracker = _GapTracker()
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

    def get_node_id(self) -> bytes:
        """Return our local 8-byte node ID."""
        return getattr(self._mesh_node, "_node_id", b"")

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
            created_ts=int(time.time()),
        )
        payload = encode_chat_message(msg)
        data_seqno = self._mesh_node.send_application_data(dest_node_id, payload)
        # Log locally as "sent"
        now = time.time()
        created_ts = int(msg.created_ts)
        self._store.add_message(
            origin_id=self.get_node_id(),
            seqno=int(data_seqno),
            channel=channel,
            nick=self._nick,
            text=text,
            ts=now,
            created_ts=created_ts,
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
        v1: Ask a peer for messages in `channel` after `since_ts`.
        """
        payload = encode_sync_request(channel=channel, nick=self._nick, since_ts=since_ts)
        self._mesh_node.send_application_data(dest_node_id, payload)

    def request_sync_last_n(
            self,
            dest_node_id: bytes,
            channel: str,
            last_n: Optional[int] = None,
    ) -> None:
        """
        v2: Ask a peer for missing messages within the last N window using seqno inventory.

        This reduces redundancy by telling the peer the max seqno we already have per origin_id.
        """
        if last_n is None:
            last_n = int(self._config.sync_last_n_messages)
        if last_n <= 0:
            return

        # Build inventory from our local last-N window for this channel
        rows = self._store.get_last_n_messages(channel, int(last_n))
        inv: Dict[str, int] = {}
        for origin_id, seqno, _channel, _nick, _text, _ts in rows:
            key = origin_id.hex()
            prev = inv.get(key)
            if prev is None or int(seqno) > prev:
                inv[key] = int(seqno)

        payload = encode_sync_request_seqno(
            channel=channel,
            nick=self._nick,
            last_n=int(last_n),
            inv=inv,
        )
        self._mesh_node.send_application_data(dest_node_id, payload)

    def request_sync_range(
            self,
            dest_node_id: bytes,
            channel: str,
            origin_id: bytes,
            start_seqno: int,
            end_seqno: int,
    ) -> None:
        """
        Targeted sync: ask a peer for a specific (origin_id, seqno range) within a channel.
        """
        if start_seqno > end_seqno:
            start_seqno, end_seqno = end_seqno, start_seqno
        if start_seqno < 0 or end_seqno < 0:
            return

        payload = encode_sync_request_range(
            channel=channel,
            nick=self._nick,
            origin_id=origin_id,
            start=int(start_seqno),
            end=int(end_seqno),
        )
        self._mesh_node.send_application_data(dest_node_id, payload)

    # --------------------------------------------------------------
    # Convenience wrappers / DB helpers / mesh callback
    # --------------------------------------------------------------

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

    def request_sync_last_n_from_peer(
            self,
            peer_nick: str,
            channel: str,
            last_n: Optional[int] = None,
    ) -> None:
        peer = self._config.peers.get(peer_nick)
        if peer is None:
            raise ValueError(f"Unknown peer nickname: {peer_nick}")
        self.request_sync_last_n(peer.node_id, channel, last_n=last_n)

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

    def get_local_channels(self, limit: int = 50) -> List[str]:
        """
        Return locally-known channel identifiers from the SQLite store.
        """
        return self._store.list_channels(limit=limit)

    def prune_db_keep_last_n_per_channel(self, keep_last_n: int) -> int:
        """
        Manually prune the local chat database. Keeps the most recent `keep_last_n`
        messages per channel/DM.

        Returns number of rows deleted.
        """
        return self._store.prune_keep_last_n_per_channel(int(keep_last_n))

    def get_discovered_nodes(self) -> Dict[str, Tuple[bytes, float]]:
        """
        Snapshot of currently-discovered nodes based on mesh routing state.

        Returns:
            {callsign: (node_id_bytes, last_seen_ts)}
        """
        state = getattr(self._mesh_node, "_routing_state", None)
        if state is None:
            return {}

        self_id = getattr(self._mesh_node, "_node_id", b"")
        results: Dict[str, Tuple[bytes, float]] = {}

        # Originators
        for node_id, entry in getattr(state, "originators", {}).items():
            if node_id == self_id:
                continue
            callsign = node_id.rstrip(b"\x00").decode("ascii", errors="ignore")
            if not callsign:
                continue
            last_seen = float(getattr(entry, "last_seen", 0.0))
            results[callsign] = (node_id, last_seen)

        # Neighbors (merge, preferring newer last_seen)
        for node_id, entry in getattr(state, "neighbors", {}).items():
            if node_id == self_id:
                continue
            callsign = node_id.rstrip(b"\x00").decode("ascii", errors="ignore")
            if not callsign:
                continue
            last_seen = float(getattr(entry, "last_seen", 0.0))
            prev = results.get(callsign)
            if prev is None or last_seen > prev[1]:
                results[callsign] = (node_id, last_seen)

        return results

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
            self._handle_sync_request(origin_id, msg)
        elif msg.msg_type == CHAT_TYPE_SYNC_RESPONSE:
            self._handle_sync_response(msg)

    def _handle_incoming_chat_message(
            self,
            origin_id: bytes,
            data_seqno: int,
            msg: ChatMessage,
            recv_ts: float,
    ) -> None:
        self._store.add_message(
            origin_id=origin_id,
            seqno=data_seqno,
            channel=msg.channel,
            nick=msg.nick,
            text=msg.text,
            ts=recv_ts,
            created_ts=int(getattr(msg, "created_ts", int(recv_ts))),
        )

        # Gap detection (local-only)
        if self._gap_tracker is not None:
            for line in self._gap_tracker.on_seqno(origin_id=origin_id, seqno=int(data_seqno), now=float(recv_ts)):
                if self._on_gap_report is not None:
                    self._on_gap_report(line)

        created_ts = float(getattr(msg, "created_ts", int(recv_ts)))
        self._on_chat_message(msg, origin_id, created_ts)

    def _handle_sync_request(
            self,
            origin_id: bytes,
            msg: ChatMessage,
    ) -> None:
        req = parse_sync_request_any(msg)
        if req is None:
            return

        max_send = int(self._config.sync_max_send_per_response)
        if max_send <= 0:
            max_send = 1

        records: List[Dict[str, object]] = []

        if req.mode == "since_ts":
            since_ts = req.since_ts
            if since_ts is None:
                return

            records_raw = self._store.get_messages_since(
                channel=msg.channel,
                since_ts=since_ts,
                limit=max_send,
            )
            for origin_bytes, seqno, _channel, nick, text, ts in records_raw:
                records.append(
                    {
                        "origin_id_hex": origin_bytes.hex(),
                        "seqno": int(seqno),
                        "nick": nick,
                        "text": text,
                        "ts": int(ts),
                    }
                )

        elif req.mode == "range":
            origin_hex = req.origin_id_hex
            if origin_hex is None:
                return
            try:
                want_origin = bytes.fromhex(origin_hex)
            except ValueError:
                return
            start_seq = int(req.start)
            end_seq = int(req.end)
            if start_seq > end_seq:
                start_seq, end_seq = end_seq, start_seq

            records_raw = self._store.get_messages_for_origin_seq_range(
                channel=msg.channel,
                origin_id=want_origin,
                start_seqno=start_seq,
                end_seqno=end_seq,
                limit=max_send,
            )
            for origin_bytes, seqno, _channel, nick, text, ts in records_raw:
                records.append(
                    {
                        "origin_id_hex": origin_bytes.hex(),
                        "seqno": int(seqno),
                        "nick": nick,
                        "text": text,
                        "ts": int(ts),
                    }
                )

        elif req.mode == "seqno":
            # Clamp request last_n to something reasonable (and to our configured default)
            last_n = int(req.last_n)
            if last_n <= 0:
                return
            # Don’t let a peer ask us for 50,000 “last messages”.
            if last_n > int(self._config.sync_last_n_messages):
                last_n = int(self._config.sync_last_n_messages)

            window_rows = self._store.get_last_n_messages(msg.channel, last_n)

            inv = req.inv
            sent = 0
            for origin_bytes, seqno, _channel, nick, text, ts in window_rows:
                if sent >= max_send:
                    break
                origin_hex = origin_bytes.hex()
                have_max = inv.get(origin_hex)
                if have_max is not None and int(seqno) <= int(have_max):
                    continue
                records.append(
                    {
                        "origin_id_hex": origin_hex,
                        "seqno": int(seqno),
                        "nick": nick,
                        "text": text,
                        "ts": int(ts),
                    }
                )
                sent += 1
        else:
            return

        response_payload = encode_sync_response(
            channel=msg.channel,
            nick=self._nick,
            records=records,
        )
        self._mesh_node.send_application_data(origin_id, response_payload)

    def _handle_sync_response(
            self,
            msg: ChatMessage,
    ) -> None:
        records = parse_sync_response(msg)
        if records is None:
            return

        applied = 0

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
            seqno_int = int(seqno_val)
            created_ts_int = int(ts_val)
            recv_ts = time.time()

            if self._store.has_message(origin_bytes, seqno_int):
                continue

            self._store.add_message(
                origin_id=origin_bytes,
                seqno=seqno_int,
                channel=msg.channel,
                nick=nick_val,
                text=text_val,
                ts=recv_ts,
                created_ts=created_ts_int,
            )
            applied += 1

            # Gap detection (local-only)
            if self._gap_tracker is not None:
                for line in self._gap_tracker.on_seqno(origin_id=origin_bytes, seqno=seqno_int, now=float(recv_ts)):
                    if self._on_gap_report is not None:
                        self._on_gap_report(line)

            chat_msg = ChatMessage(

                msg_type=CHAT_TYPE_MESSAGE,
                channel=msg.channel,
                nick=nick_val,
                text=text_val,
                created_ts=created_ts_int,
            )
            self._on_chat_message(chat_msg, origin_bytes, float(created_ts_int))

        if applied > 0 and self._on_sync_applied is not None:
            self._on_sync_applied(msg.channel, applied)
