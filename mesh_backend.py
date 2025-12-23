# mesh_backend.py

from __future__ import annotations

import queue
import threading
import time
import json
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple

from ardop_link import ArdopLinkError

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
    timestamp: float  # created time (unix seconds)
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
    # List of (origin_id, seqno, channel, nick, text, created_ts)
    messages: List[Tuple[bytes, int, str, str, str, float]]


# ============================================================
# Sync retry scheduler (bounded exponential backoff)
# ============================================================

@dataclass
class _SyncRetryState:
    peer_label: str
    channel: str
    dest_node_id: bytes
    last_n: int
    attempts: int = 0
    next_due_ts: float = 0.0
    last_send_ts: float = 0.0
    gave_up: bool = False


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

    def request_sync_for_channel(self, channel: str) -> None:
        """Request a network sync (last-N inventory) for a channel/DM tab."""
        raise NotImplementedError

    def prune_db(self) -> None:
        """Prune the local chat database (manual, user-confirmed)."""
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
        # Per-peer/per-channel sync cooldown tracking
        self._last_sync_time: Dict[Tuple[str, str], float] = {}
        # Sync retry/backoff scheduler state
        self._sync_retry: Dict[Tuple[str, str], _SyncRetryState] = {}
        self._sync_retry_lock = threading.Lock()
        self._sync_retry_thread = threading.Thread(
            target=self._sync_retry_loop,
            name="MeshBackendSyncRetry",
            daemon=True,
        )
        self._sync_retry_thread.start()

        # Instantiate client with callback into UI queue
        self._client = MeshChatClient(
            config=config,
            on_chat_message=self._on_chat_message,
            on_sync_applied=self._on_sync_applied,
            on_gap_report=self._on_gap_report,
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

    # ----------------------------------------------------------
    # Structured diagnostics (Feature #2)
    # ----------------------------------------------------------

    def _build_diagnostics_snapshot(self) -> dict:
        """Build a machine-stable diagnostics snapshot from existing runtime data only."""
        now = time.time()

        # Node identity
        callsign = str(getattr(getattr(self._config, "mesh_node_config", None), "callsign", "") or "")
        try:
            node_id = self._client.get_node_id()
        except (AttributeError, OSError, ValueError, TypeError):
            node_id = b""

        startup_error = getattr(self._client, "_startup_error", None)
        if not isinstance(startup_error, str) or not startup_error:
            startup_error = ""

        # Mesh routing state (best-effort introspection; no behavior changes)
        originators_count = 0
        neighbors_count = 0
        try:
            mesh_node = getattr(self._client, "_mesh_node", None)
            state = getattr(mesh_node, "_routing_state", None)
            originators = getattr(state, "originators", {}) if state is not None else {}
            neighbors = getattr(state, "neighbors", {}) if state is not None else {}
            if isinstance(originators, dict):
                originators_count = len(originators)
            if isinstance(neighbors, dict):
                neighbors_count = len(neighbors)
        except (AttributeError, TypeError, ValueError):
            originators_count = 0
            neighbors_count = 0

        # Discovered nodes (existing API)
        discovered_list: list[dict] = []
        try:
            disc = self._client.get_discovered_nodes()
        except (AttributeError, OSError, ValueError, TypeError):
            disc = {}

        if isinstance(disc, dict):
            for cs, tup in disc.items():
                try:
                    node_id_b, last_seen = tup
                except (TypeError, ValueError):
                    continue
                if not isinstance(cs, str):
                    continue
                age_s = None
                if isinstance(last_seen, (int, float)) and float(last_seen) > 0:
                    age_s = max(0.0, now - float(last_seen))
                discovered_list.append(
                    {
                        "callsign": cs,
                        "node_id_hex": node_id_b.hex() if isinstance(node_id_b, (bytes, bytearray)) else "",
                        "last_seen_age_s": age_s,
                    }
                )
            discovered_list.sort(key=lambda d: str(d.get("callsign") or ""))

        # Per-link metrics (Feature #1)
        try:
            link_metrics = self._client.get_link_metrics()
        except (AttributeError, OSError, ValueError, TypeError):
            link_metrics = []
        if not isinstance(link_metrics, list):
            link_metrics = []

        # Sync / retry state (already present in backend)
        retries: list[dict] = []
        with self._sync_retry_lock:
            items = list(self._sync_retry.items())
        for (_k, st) in items:
            try:
                due_in_s = max(0.0, float(st.next_due_ts) - now)
            except (TypeError, ValueError):
                due_in_s = 0.0
            retries.append(
                {
                    "peer_label": str(getattr(st, "peer_label", "") or ""),
                    "channel": str(getattr(st, "channel", "") or ""),
                    "attempts": int(getattr(st, "attempts", 0) or 0),
                    "due_in_s": due_in_s,
                    "gave_up": bool(getattr(st, "gave_up", False)),
                    "last_n": int(getattr(st, "last_n", 0) or 0),
                }
            )
        retries.sort(key=lambda d: (str(d.get("channel") or ""), str(d.get("peer_label") or "")))

        # Local channels (from our cached view; derived from ChatStore via backend refresh)
        local_channels = list(self._last_channels) if isinstance(self._last_channels, list) else []

        snap = {
            "diag_version": 1,
            "ts": int(now),
            "node": {
                "callsign": callsign,
                "node_id_hex": node_id.hex() if isinstance(node_id, (bytes, bytearray)) else "",
                "startup_error": startup_error,
            },
            "mesh": {
                "originators_count": int(originators_count),
                "neighbors_count": int(neighbors_count),
                "discovered_count": int(len(discovered_list)),
                "discovered_nodes": discovered_list[:25],  # cap for RF readability
            },
            "links": link_metrics,
            "sync": {
                "cooldowns_tracked": int(len(self._last_sync_time)) if isinstance(self._last_sync_time, dict) else 0,
                "retries_tracked": int(len(retries)),
                "retries": retries[:25],  # cap
            },
            "db": {
                "local_channels_count": int(len(local_channels)),
                "local_channels": local_channels[:25],  # cap
            },
        }
        return snap

    @staticmethod
    def _format_diagnostics_text(snap: dict) -> list[str]:
        """Render a diagnostics snapshot as compact, RF-friendly lines."""
        lines: list[str] = []
        if not isinstance(snap, dict):
            return lines

        ts = snap.get("ts")
        dv = snap.get("diag_version")
        node = snap.get("node") if isinstance(snap.get("node"), dict) else {}
        mesh = snap.get("mesh") if isinstance(snap.get("mesh"), dict) else {}
        sync = snap.get("sync") if isinstance(snap.get("sync"), dict) else {}
        db = snap.get("db") if isinstance(snap.get("db"), dict) else {}

        callsign = str(node.get("callsign") or "")
        node_hex = str(node.get("node_id_hex") or "")
        startup_err = str(node.get("startup_error") or "")

        lines.append(f"DIAG v{dv} ts={ts} callsign={callsign} node_id={node_hex}")
        if startup_err:
            cleaned = " ".join(startup_err.split())
            if len(cleaned) > 160:
                cleaned = cleaned[:157] + "..."
            lines.append(f"NODE startup_error=\"{cleaned}\"")

        lines.append(
            "MESH "
            f"discovered={int(mesh.get('discovered_count', 0) or 0)} "
            f"originators={int(mesh.get('originators_count', 0) or 0)} "
            f"neighbors={int(mesh.get('neighbors_count', 0) or 0)}"
        )

        # Discovered nodes (callsign + age)
        dn = mesh.get("discovered_nodes")
        if isinstance(dn, list) and dn:
            parts = []
            for d in dn[:10]:
                if not isinstance(d, dict):
                    continue
                cs = str(d.get("callsign") or "")
                age = d.get("last_seen_age_s")
                if cs:
                    if isinstance(age, (int, float)):
                        parts.append(f"{cs}({float(age):.0f}s)")
                    else:
                        parts.append(cs)
            if parts:
                lines.append("NODES " + " ".join(parts))

        # DB channels
        ch = db.get("local_channels")
        if isinstance(ch, list) and ch:
            shown = [str(x) for x in ch[:10] if str(x)]
            if shown:
                lines.append("DB channels=" + ",".join(shown))

        # Sync state
        lines.append(
            "SYNC "
            f"cooldowns={int(sync.get('cooldowns_tracked', 0) or 0)} "
            f"retries={int(sync.get('retries_tracked', 0) or 0)}"
        )
        rlist = sync.get("retries")
        if isinstance(rlist, list) and rlist:
            for r in rlist[:10]:
                if not isinstance(r, dict):
                    continue
                peer = str(r.get("peer_label") or "")
                chan = str(r.get("channel") or "")
                att = int(r.get("attempts", 0) or 0)
                due = r.get("due_in_s")
                gu = bool(r.get("gave_up", False))
                due_s = f"{float(due):.1f}s" if isinstance(due, (int, float)) else "?"
                lines.append(f"RETRY peer={peer} chan={chan} attempts={att} due_in={due_s} gave_up={1 if gu else 0}")

        return lines

    @staticmethod
    def _format_diagnostics_json(snap: dict) -> str:
        """Render snapshot as a single stable JSON line."""
        try:
            return json.dumps(snap, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return "{}"

    def _emit_structured_diagnostics(self) -> None:
        """Emit structured diagnostics to the UI queue as StatusEvents (text + JSON)."""
        snap = self._build_diagnostics_snapshot()
        for line in self._format_diagnostics_text(snap):
            self._emit_status(line)

        # Machine-stable one-liner for parsing/log collection
        self._emit_status("DIAG_JSON " + self._format_diagnostics_json(snap))

    @staticmethod
    def _format_link_metrics(m: dict) -> str:
        """Format a best-effort per-link metrics snapshot into a single status line.

        This is intentionally defensive: link metric dictionaries may evolve and should
        never break the GUI/daemon if a key is missing.
        """
        try:
            name = str(m.get("name") or m.get("link_name") or "link")
        except (AttributeError, TypeError, ValueError):
            name = "link"

        try:
            ltype = str(m.get("type") or m.get("link_type") or "unknown")
        except (AttributeError, TypeError, ValueError):
            ltype = "unknown"

        running = bool(m.get("running", False))
        connected = bool(m.get("connected", False))

        frames_tx = int(m.get("frames_tx", 0) or 0)
        frames_rx = int(m.get("frames_rx", 0) or 0)
        bytes_tx = int(m.get("bytes_tx", 0) or 0)
        bytes_rx = int(m.get("bytes_rx", 0) or 0)

        connect_attempts = int(m.get("connect_attempts", 0) or 0)
        connect_successes = int(m.get("connect_successes", 0) or 0)
        disconnects = int(m.get("disconnects", 0) or 0)
        drops = int(m.get("drops", 0) or 0)

        last_rx_ts = m.get("last_rx_ts")
        rx_age_s = None
        if isinstance(last_rx_ts, (int, float)) and last_rx_ts > 0:
            rx_age_s = max(0.0, time.time() - float(last_rx_ts))

        last_tx_ts = m.get("last_tx_ts")
        tx_age_s = None
        if isinstance(last_tx_ts, (int, float)) and last_tx_ts > 0:
            tx_age_s = max(0.0, time.time() - float(last_tx_ts))

        parts = [
            f"[LINK] {name} ({ltype})",
            f"running={1 if running else 0}",
            f"connected={1 if connected else 0}",
            f"tx={frames_tx}f/{bytes_tx}B",
            f"rx={frames_rx}f/{bytes_rx}B",
        ]
        if rx_age_s is not None:
            parts.append(f"rx_age={rx_age_s:.1f}s")
        if tx_age_s is not None:
            parts.append(f"tx_age={tx_age_s:.1f}s")
        if connect_attempts or connect_successes:
            parts.append(f"conn={connect_successes}/{connect_attempts}")
        if disconnects:
            parts.append(f"disc={disconnects}")
        if drops:
            parts.append(f"drops={drops}")

        # Multiplex links may include nested per-link metrics.
        child_links = m.get("child_links")
        if child_links is None:
            child_links = m.get("links")
        if isinstance(child_links, list) and child_links:
            # Summarize connection state without spamming the status window.
            states = []
            for c in child_links[:6]:  # cap
                try:
                    cn = str(c.get("name") or c.get("link_name") or "?")
                    cc = "1" if bool(c.get("connected", False)) else "0"
                    states.append(f"{cn}:{cc}")
                except (AttributeError, TypeError, ValueError):
                    continue
            if states:
                parts.append("children=" + ",".join(states))

        last_error = m.get("last_error")
        if isinstance(last_error, str) and last_error:
            # keep it readable; the raw exception repr can be huge
            cleaned = " ".join(last_error.split())
            if len(cleaned) > 160:
                cleaned = cleaned[:157] + "..."
            parts.append(f'err="{cleaned}"')

        return " ".join(parts)

    def _on_sync_applied(self, channel: str, applied_count: int) -> None:
        """Callback from MeshChatClient when a SYNC_RESPONSE is applied to the DB."""
        self._emit_status(f"Sync applied for {channel}: {applied_count} new message(s)")
        if applied_count > 0:
            self._clear_sync_retries_for_channel(channel)
        # Sync can introduce new channels/DMs; refresh left-list.
        self._refresh_channels_from_db()

    def _on_gap_report(self, text: str) -> None:
        """Callback from MeshChatClient when a gap report is generated.

        Policy:
          - Only act on confirmed gaps (text contains " (confirmed)").
          - Coalesce nearby/overlapping ranges into fewer requests.
          - Chunk large ranges to keep requests bounded.
          - Rate-limit to stay polite on RF.
        """
        self._emit_status(text)

        # Example line:
        #   KD9YQK-1 missing seq 142–147, 150 (confirmed)
        if " (confirmed)" not in text:
            return

        marker = " missing seq "
        idx = text.find(marker)
        if idx <= 0:
            return

        callsign = text[:idx].strip()
        if not callsign:
            return

        node_id = self._discovered_node_ids.get(callsign)
        if node_id is None:
            return

        cfg = self._config
        if not bool(getattr(cfg, "targeted_sync_enabled", True)):
            return

        # Parse ranges portion
        ranges_part = text[idx + len(marker):]
        paren_idx = ranges_part.find(" (")
        if paren_idx >= 0:
            ranges_part = ranges_part[:paren_idx]
        ranges_part = ranges_part.strip()
        if not ranges_part:
            return

        # Rate limit per-origin for gap-triggered requests
        min_interval = float(getattr(cfg, "sync_min_sync_interval_seconds", 30.0))
        if min_interval < 0.0:
            min_interval = 0.0

        now = time.time()
        key = (callsign, "__gap_range__")
        last_ts = self._last_sync_time.get(key)
        if last_ts is not None and (now - last_ts) < min_interval:
            return

        # Convert "142–147, 150" into list of (start,end)
        items = [p.strip() for p in ranges_part.split(",") if p.strip()]
        parsed: List[Tuple[int, int]] = []
        for part in items:
            if "–" in part:
                a, b = part.split("–", 1)
            elif "-" in part:
                a, b = part.split("-", 1)
            else:
                a, b = part, part
            try:
                s = int(a.strip())
                e = int(b.strip())
            except ValueError:
                continue
            if s < 0 or e < 0:
                continue
            if e < s:
                s, e = e, s
            parsed.append((s, e))

        if not parsed:
            return

        merge_distance = int(getattr(cfg, "targeted_sync_merge_distance", 0))
        if merge_distance < 0:
            merge_distance = 0

        max_range_len = int(getattr(cfg, "targeted_sync_max_range_len", 50))
        if max_range_len < 1:
            max_range_len = 1

        max_requests = int(getattr(cfg, "targeted_sync_max_requests_per_trigger", 3))
        if max_requests < 1:
            max_requests = 1

        # --- Coalesce ranges ---
        parsed.sort(key=lambda t: t[0])
        merged: List[Tuple[int, int]] = []
        cur_s, cur_e = parsed[0]
        for s, e in parsed[1:]:
            if s <= (cur_e + 1 + merge_distance):
                if e > cur_e:
                    cur_e = e
            else:
                merged.append((cur_s, cur_e))
                cur_s, cur_e = s, e
        merged.append((cur_s, cur_e))

        # --- Chunk merged ranges to bounded requests ---
        chunks: List[Tuple[int, int]] = []
        for s, e in merged:
            if (e - s + 1) <= max_range_len:
                chunks.append((s, e))
                continue
            cur = s
            while cur <= e:
                end = cur + max_range_len - 1
                if end > e:
                    end = e
                chunks.append((cur, end))
                cur = end + 1

        chunks = chunks[:max_requests]

        try:
            for s, e in chunks:
                self._client.request_sync_range(
                    dest_node_id=node_id,
                    channel="#general",
                    origin_id=node_id,
                    start_seqno=s,
                    end_seqno=e,
                )
            self._last_sync_time[key] = now
        except (OSError, ValueError, ArdopLinkError) as exc:
            self._emit_status(f"Targeted sync request failed for {callsign}: {exc}")

    def _status_loop(self) -> None:
        while self._running:
            time.sleep(self._status_interval)
            if not self._running:
                return

            # Feature #2: structured diagnostics snapshot (human + machine stable)
            self._emit_structured_diagnostics()

            # Per-link health/metrics snapshot (best-effort, no protocol changes)
            try:
                metrics_list = self._client.get_link_metrics()
            except (OSError, ValueError, AttributeError, TypeError):
                metrics_list = []

            for m in metrics_list:
                self._emit_status(self._format_link_metrics(m))

    def _nodes_loop(self) -> None:
        """Periodically snapshot routing state and notify the GUI."""
        while self._running:
            time.sleep(1.0)
            if not self._running:
                return

            discovered = self._client.get_discovered_nodes()
            # Save mapping for DM fallback
            self._discovered_node_ids = {k: v[0] for k, v in discovered.items()}

            prev_nodes = set(self._last_nodes)
            nodes = sorted(discovered.keys())
            if nodes != self._last_nodes:
                self._last_nodes = nodes
                self._ui_queue.put(NodeListEvent(nodes=nodes))
            new_peers = sorted(set(nodes) - prev_nodes)
            if new_peers:
                cfg = self._config
                if getattr(cfg, "sync_enabled", True) and getattr(cfg, "sync_auto_sync_on_new_peer", True):
                    channel = "#general"
                    last_n = int(getattr(cfg, "sync_last_n_messages", 200))
                    min_interval = float(getattr(cfg, "sync_min_sync_interval_seconds", 30.0))
                    now = time.time()
                    for callsign in new_peers:
                        node_id = self._discovered_node_ids.get(callsign)
                        if not node_id:
                            continue
                        key = (callsign, channel)
                        last_ts = self._last_sync_time.get(key)
                        if last_ts is not None and (now - last_ts) < min_interval:
                            continue
                        try:
                            self._client.request_sync_last_n(dest_node_id=node_id, channel=channel, last_n=last_n)
                            self._last_sync_time[key] = now
                            self._emit_status(f"Auto-sync requested for {channel} from {callsign}")
                            self._schedule_sync_retry(peer_label=callsign, channel=channel, dest_node_id=node_id,
                                                      last_n=last_n)
                        except (OSError, ValueError, ArdopLinkError) as exc:
                            self._emit_status(f"Auto-sync request failed for {channel} from {callsign}: {exc}")

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

    def request_sync_for_channel(self, channel: str) -> None:
        """
        Trigger a network sync for the given channel/DM tab.

        - For DMs ("@CALLSIGN"): sync is requested from that specific node (if known).
        - For normal channels ("#general"): sync is requested from the default peer.
        """
        cfg = self._config
        if not getattr(cfg, "sync_enabled", True):
            return

        last_n = int(getattr(cfg, "sync_last_n_messages", 200))
        if last_n <= 0:
            return

        min_interval = float(getattr(cfg, "sync_min_sync_interval_seconds", 30.0))
        if min_interval < 0.0:
            min_interval = 0.0

        now = time.time()

        # DM
        if channel.startswith("@") and len(channel) > 1:
            callsign = channel[1:]
            node_id = self._discovered_node_ids.get(callsign)
            if node_id is None:
                self._emit_status(f"Cannot sync {channel}: destination not discovered yet.")
                return

            key = (callsign, channel)
            last_ts = self._last_sync_time.get(key)
            if last_ts is not None and (now - last_ts) < min_interval:
                return

            try:
                self._client.request_sync_last_n(dest_node_id=node_id, channel=channel, last_n=last_n)
                self._last_sync_time[key] = now
                self._emit_status(f"Sync requested for {channel} from {callsign}")
                self._schedule_sync_retry(peer_label=callsign, channel=channel, dest_node_id=node_id, last_n=last_n)
            except (OSError, ValueError, ArdopLinkError) as exc:
                self._emit_status(f"Sync request failed for {channel}: {exc}")
            return

        # Channel
        try:
            default_peer = cfg.peers[self._default_peer_nick]
        except KeyError:
            self._emit_status("Cannot sync: default peer is not configured.")
            return

        peer_label = self._default_peer_nick
        key = (peer_label, channel)
        last_ts = self._last_sync_time.get(key)
        if last_ts is not None and (now - last_ts) < min_interval:
            return

        try:
            self._client.request_sync_last_n(dest_node_id=default_peer.node_id, channel=channel, last_n=last_n)
            self._last_sync_time[key] = now
            self._emit_status(f"Sync requested for {channel} from {peer_label}")
            self._schedule_sync_retry(peer_label=peer_label, channel=channel, dest_node_id=default_peer.node_id,
                                      last_n=last_n)
        except (OSError, ValueError, ArdopLinkError) as exc:
            self._emit_status(f"Sync request failed for {channel}: {exc}")

    def prune_db(self) -> None:
        """
        Manually prune the local SQLite chat database.

        Policy: keep the most recent N messages per channel/DM, where N is
        taken from chat.sync.last_n_messages (or its default).
        """
        keep_last_n = int(getattr(self._config, "sync_last_n_messages", 200))
        if keep_last_n < 1:
            keep_last_n = 1

        try:
            deleted = self._client.prune_db_keep_last_n_per_channel(keep_last_n)
        except (OSError, ValueError, ArdopLinkError) as exc:
            self._emit_status(f"DB prune failed: {exc}")
            return

        self._emit_status(f"DB pruned: deleted {deleted} rows (kept last {keep_last_n} per channel).")
        self._refresh_channels_from_db()

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

    # ----------------------------------------------------------
    # Sync retry/backoff scheduler
    # ----------------------------------------------------------

    def _schedule_sync_retry(
            self,
            peer_label: str,
            channel: str,
            dest_node_id: bytes,
            last_n: int,
    ) -> None:
        """Schedule bounded retry attempts for a sync request that was already initiated.

        This does not introduce new sync behavior; it only retries after a request
        has been made (manual or auto-sync). Rate limiting and backoff are handled here.
        """
        key = (str(peer_label), str(channel))
        now = time.time()
        with self._sync_retry_lock:
            state = self._sync_retry.get(key)
            if state is None:
                state = _SyncRetryState(
                    peer_label=str(peer_label),
                    channel=str(channel),
                    dest_node_id=bytes(dest_node_id),
                    last_n=int(last_n),
                    attempts=0,
                    next_due_ts=now,
                    last_send_ts=0.0,
                    gave_up=False,
                )
                self._sync_retry[key] = state
            else:
                # Reset scheduling to be responsive to a new explicit request
                state.dest_node_id = bytes(dest_node_id)
                state.last_n = int(last_n)
                state.attempts = 0
                state.gave_up = False
                state.next_due_ts = now

    def _clear_sync_retries_for_channel(self, channel: str) -> None:
        """Clear pending retries for a channel once we observe progress."""
        with self._sync_retry_lock:
            to_del = [k for k, v in self._sync_retry.items() if v.channel == channel]
            for k in to_del:
                del self._sync_retry[k]

    @staticmethod
    def _compute_backoff_seconds(state: _SyncRetryState) -> float:
        # 5s * 2^attempts, capped at 120s
        base = 5.0 * (2.0 ** float(state.attempts))
        if base > 120.0:
            base = 120.0

        # Deterministic jitter in [0, 1.0) seconds (no random import)
        try:
            b = state.dest_node_id + state.channel.encode("utf-8", errors="ignore")
            jitter = float(sum(b) % 1000) / 1000.0
        except (UnicodeError, TypeError, AttributeError):
            jitter = 0.0
        return base + jitter

    def _sync_retry_loop(self) -> None:
        """Background loop that retries previously requested syncs with backoff."""
        while self._running:
            time.sleep(0.5)
            if not self._running:
                return

            now = time.time()
            due: List[_SyncRetryState] = []

            with self._sync_retry_lock:
                for st in self._sync_retry.values():
                    if st.gave_up:
                        continue
                    if st.next_due_ts <= now:
                        due.append(st)

            for st in due:
                # Stop after a bounded number of attempts to avoid RF spam.
                if st.attempts >= 6:
                    if not st.gave_up:
                        st.gave_up = True
                        self._emit_status(f"Sync retry gave up for {st.channel} from {st.peer_label}")
                    continue

                try:
                    self._client.request_sync_last_n(
                        dest_node_id=st.dest_node_id,
                        channel=st.channel,
                        last_n=int(st.last_n),
                    )
                except (OSError, ValueError, ArdopLinkError) as exc:
                    # We still back off and retry; just report minimally.
                    self._emit_status(f"Sync retry failed for {st.channel} from {st.peer_label}: {exc}")

                st.last_send_ts = now
                st.attempts += 1
                delay = self._compute_backoff_seconds(st)
                st.next_due_ts = now + delay
