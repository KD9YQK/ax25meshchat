#!/usr/bin/env python3
"""
fake_ardopc.py — framing-compatible test harness for ardop_link.py

Framing:
    [u16be_len] + payload

Fake OGM format (matches mesh_node.py implementation):
    header (16 bytes):
      [ver:1][msg_type:1][flags:1][ttl:1][origin_id:8][seqno:u32be]
    body (>=9 bytes for OGM):
      [prev_hop:8][link_metric:1]
"""

from __future__ import annotations

import argparse
import binascii
import json
import random
import heapq
import socket
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from chat_protocol import CHAT_TYPE_SYNC_REQUEST, decode_chat_message, parse_sync_request_any, ChatMessage, \
    CHAT_TYPE_MESSAGE, encode_chat_message

MAX_FRAME_LEN = 65535
READ_CHUNK = 4096

MESH_VERSION = 0x01
MESH_MSG_DATA = 0x00
MESH_MSG_OGM = 0x01

MESH_FLAG_COMPRESSED = 0x01
MESH_FLAG_ENCRYPTED = 0x02


def _u16be(n: int) -> bytes:
    return bytes([(n >> 8) & 0xFF, n & 0xFF])


def _u32be(n: int) -> bytes:
    return bytes([(n >> 24) & 0xFF, (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF])


def _frame(payload: bytes) -> bytes:
    if len(payload) > MAX_FRAME_LEN:
        raise ValueError("payload too large for 16-bit length prefix")
    return _u16be(len(payload)) + payload


def _hex(b: bytes) -> str:
    return binascii.hexlify(b).decode("ascii")


def _unhex(s: str) -> bytes:
    s = s.strip().replace(" ", "")
    if s == "":
        return b""
    return binascii.unhexlify(s)


def _ascii8(s: str) -> bytes:
    """
    8-byte node_id field: ASCII, padded with NULs, truncated to 8 bytes.
    """
    b = s.encode("ascii", errors="strict")
    if len(b) >= 8:
        return b[:8]
    return b + (b"\x00" * (8 - len(b)))


def _ascii_from_id8(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def _try_decode_sync_request(mesh_payload: bytes) -> Optional[str]:
    """
    Best-effort decoder for CHAT_TYPE_SYNC_REQUEST embedded in a mesh DATA frame.
    Returns a human-readable one-liner, or None if not a sync request / not decodable.
    """
    if len(mesh_payload) < 16 + 12:
        return None

    ver = mesh_payload[0]
    msg_type = mesh_payload[1]
    flags = mesh_payload[2]
    ttl = mesh_payload[3]
    origin_id8 = mesh_payload[4:12]
    seqno = int.from_bytes(mesh_payload[12:16], byteorder="big", signed=False)

    if ver != MESH_VERSION or msg_type != MESH_MSG_DATA:
        return None

    body = mesh_payload[16:]
    if len(body) < 12:
        return None

    dest_id8 = body[0:8]
    data_seq = int.from_bytes(body[8:12], byteorder="big", signed=False)
    app_bytes = body[12:]

    origin = _ascii_from_id8(origin_id8)
    dest = _ascii_from_id8(dest_id8)

    if (flags & MESH_FLAG_ENCRYPTED) != 0:
        return f"SYNC_REQUEST origin={origin} dest={dest} ttl={ttl} seqno={seqno} data_seq={data_seq} [encrypted]"

    if (flags & MESH_FLAG_COMPRESSED) != 0:
        try:
            app_bytes = zlib.decompress(app_bytes)
        except zlib.error:
            return None

    chat_msg = decode_chat_message(app_bytes)
    if chat_msg is None or chat_msg.msg_type != CHAT_TYPE_SYNC_REQUEST:
        return None

    req = parse_sync_request_any(chat_msg)
    if req is None:
        # The test harness may be newer/older than the runtime protocol helpers.
        # Best-effort: try to interpret text JSON directly for newer modes.
        try:
            obj = json.loads(chat_msg.text)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        if obj.get("mode") != "range":
            return None
        origin_id_hex = obj.get("origin_id_hex")
        start = obj.get("start")
        end = obj.get("end")
        if not isinstance(origin_id_hex, str) or not isinstance(start, int) or not isinstance(end, int):
            return None
        return (
            f"SYNC_REQUEST(range) origin={origin} dest={dest} channel={chat_msg.channel!r} nick={chat_msg.nick!r} "
            f"target_origin={origin_id_hex} seq={start}-{end}"
        )

    if req.mode == "since_ts":
        return (
            f"SYNC_REQUEST(v1 since_ts) origin={origin} dest={dest} channel={chat_msg.channel!r} "
            f"nick={chat_msg.nick!r} since_ts={req.since_ts}"
        )

    if req.mode == "range":
        # If parse_sync_request_any supports it, display the mode cleanly.
        # (Current codebase may decode it directly into SyncRequest, or not.)
        try:
            obj = json.loads(chat_msg.text)
        except json.JSONDecodeError:
            obj = {}
        origin_id_hex = obj.get("origin_id_hex") if isinstance(obj, dict) else None
        start = obj.get("start") if isinstance(obj, dict) else None
        end = obj.get("end") if isinstance(obj, dict) else None
        if isinstance(origin_id_hex, str) and isinstance(start, int) and isinstance(end, int):
            return (
                f"SYNC_REQUEST(range) origin={origin} dest={dest} channel={chat_msg.channel!r} nick={chat_msg.nick!r} "
                f"target_origin={origin_id_hex} seq={start}-{end}"
            )
        return (
            f"SYNC_REQUEST(range) origin={origin} dest={dest} channel={chat_msg.channel!r} nick={chat_msg.nick!r}"
        )

    inv_items = sorted(req.inv.items())
    preview = ", ".join([f"{k}:{v}" for k, v in inv_items[:5]])
    more = "" if len(inv_items) <= 5 else f" (+{len(inv_items) - 5} more)"
    return (
        f"SYNC_REQUEST(v2 seqno) origin={origin} dest={dest} channel={chat_msg.channel!r} nick={chat_msg.nick!r} "
        f"last_n={req.last_n} inv={len(inv_items)} [{preview}{more}]"
    )


def build_mesh_header(*, msg_type: int, flags: int, ttl: int, origin_id8: bytes, seqno: int) -> bytes:
    if len(origin_id8) != 8:
        raise ValueError("origin_id8 must be exactly 8 bytes")
    return bytes([
        MESH_VERSION & 0xFF,
        msg_type & 0xFF,
        flags & 0xFF,
        ttl & 0xFF,
    ]) + origin_id8 + _u32be(seqno)


def build_fake_ogm(*, origin: str, seqno: int, ttl: int = 5, link_metric: int = 0xFF) -> bytes:
    """
    OGM payload:
      header(16) + prev_hop(8) + link_metric(1)
    """
    origin8 = _ascii8(origin)
    header = build_mesh_header(msg_type=MESH_MSG_OGM, flags=0x00, ttl=ttl, origin_id8=origin8, seqno=seqno)
    prev_hop = origin8  # direct neighbor illusion
    lm = bytes([link_metric & 0xFF])
    return header + prev_hop + lm


def build_fake_data(
        *,
        origin: str,
        dest: str,
        seqno: int,
        ttl: int,
        app_payload: bytes,
        compress: bool = True,
) -> bytes:
    """
    DATA payload (matches mesh_node.py):
      header(16) + dest_id(8) + data_seqno(u32be) + [payload...]
    This helper sends unencrypted payloads (no nonce/ciphertext).
    """
    origin8 = _ascii8(origin)
    dest8 = _ascii8(dest)

    flags = 0
    payload_to_send = app_payload
    if compress and app_payload:
        compressed = zlib.compress(app_payload)
        if len(compressed) < len(app_payload):
            payload_to_send = compressed
            flags |= MESH_FLAG_COMPRESSED

    header = build_mesh_header(
        msg_type=MESH_MSG_DATA,
        flags=flags,
        ttl=ttl,
        origin_id8=origin8,
        seqno=seqno,
    )
    body = dest8 + _u32be(seqno) + payload_to_send
    return header + body


@dataclass
class ClientState:
    addr: Tuple[str, int]
    rx_buf: bytearray = field(default_factory=bytearray)
    last_rx: float = field(default_factory=time.time)
    last_tx: float = field(default_factory=time.time)


class FakeArdopServer:
    def __init__(
            self,
            host: str,
            port: int,
            *,
            echo: bool,
            broadcast: bool,
            drop_rate: float,
            base_delay_ms: int,
            jitter_ms: int,
            reorder_rate: float,
            reorder_max_delay_ms: int,
            fake_ogm: bool,
            fake_ogm_id: str,
            fake_ogm_interval_s: float,
            fake_ogm_ttl: int,
            fake_ogm_metric: int,
    ) -> None:
        self._host = host
        self._port = port
        self._echo = echo
        self._broadcast = broadcast

        if drop_rate < 0.0:
            drop_rate = 0.0
        if drop_rate > 1.0:
            drop_rate = 1.0
        if base_delay_ms < 0:
            base_delay_ms = 0
        if jitter_ms < 0:
            jitter_ms = 0

        self._drop_rate = float(drop_rate)
        self._base_delay_ms = int(base_delay_ms)
        self._jitter_ms = int(jitter_ms)

        if reorder_rate < 0.0:
            reorder_rate = 0.0
        if reorder_rate > 1.0:
            reorder_rate = 1.0
        if reorder_max_delay_ms < 0:
            reorder_max_delay_ms = 0

        self._reorder_rate = float(reorder_rate)
        self._reorder_max_delay_ms = int(reorder_max_delay_ms)

        self._txq_lock = threading.Lock()
        self._txq: list[tuple[float, socket.socket, bytes]] = []
        self._txq_wake = threading.Event()

        self._fake_ogm = fake_ogm
        self._fake_ogm_id = fake_ogm_id
        self._fake_ogm_interval_s = fake_ogm_interval_s
        self._fake_ogm_ttl = fake_ogm_ttl
        self._fake_ogm_metric = fake_ogm_metric
        self._fake_ogm_seqno = 1

        self._srv_sock: Optional[socket.socket] = None
        self._stop = threading.Event()

        self._clients_lock = threading.Lock()
        self._clients: Dict[socket.socket, ClientState] = {}

    def _maybe_delay(self) -> None:
        delay_ms = self._base_delay_ms
        if self._jitter_ms > 0:
            delay_ms += int(random.uniform(0.0, float(self._jitter_ms)))
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)

    def _should_drop(self) -> bool:
        if self._drop_rate <= 0.0:
            return False
        return random.random() < self._drop_rate

    def _compute_total_delay_s(self) -> float:
        """Compute total delay in seconds including base delay, jitter, and optional reorder delay."""
        delay_ms = self._base_delay_ms
        if self._jitter_ms > 0:
            delay_ms += int(random.uniform(0.0, float(self._jitter_ms)))
        # Reorder simulation: probabilistically add extra delay to some frames so later frames can overtake.
        if self._reorder_rate > 0.0 and random.random() < self._reorder_rate:
            if self._reorder_max_delay_ms > 0:
                delay_ms += int(random.uniform(0.0, float(self._reorder_max_delay_ms)))
        if delay_ms <= 0:
            return 0.0
        return float(delay_ms) / 1000.0

    def _tx_scheduler_loop(self) -> None:
        """Background loop that sends scheduled frames when their send_at time arrives."""
        while not self._stop.is_set():
            now = time.time()
            due: list[tuple[float, socket.socket, bytes]] = []
            next_send_at: Optional[float] = None

            with self._txq_lock:
                if self._txq:
                    # Partition due items; keep the rest.
                    remaining: list[tuple[float, socket.socket, bytes]] = []
                    for send_at, sock, data in self._txq:
                        if send_at <= now:
                            due.append((send_at, sock, data))
                        else:
                            remaining.append((send_at, sock, data))
                            if next_send_at is None or send_at < next_send_at:
                                next_send_at = send_at
                    self._txq = remaining

                # Reset wake event after we snapshot queue.
                self._txq_wake.clear()

            # Send due items outside the lock.
            for _send_at, sock, data in due:
                with self._clients_lock:
                    st = self._clients.get(sock)
                if st is None:
                    continue
                try:
                    sock.sendall(data)
                    st.last_tx = time.time()
                except OSError:
                    with self._clients_lock:
                        if sock in self._clients:
                            self._drop_client(sock)

            # Sleep until next event / due time.
            if next_send_at is None:
                # Nothing queued; wait for new work.
                self._txq_wake.wait(0.25)
            else:
                wait_s = next_send_at - time.time()
                if wait_s <= 0.0:
                    continue
                # Wake early if new tasks arrive.
                self._txq_wake.wait(wait_s if wait_s < 0.25 else 0.25)

    def start(self) -> None:
        self._srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv_sock.bind((self._host, self._port))
        self._srv_sock.listen(10)
        print(f"[fake_ardopc] listening on {self._host}:{self._port}")
        threading.Thread(target=self._accept_loop, daemon=True).start()

        threading.Thread(target=self._tx_scheduler_loop, daemon=True).start()

        if self._fake_ogm:
            threading.Thread(target=self._fake_ogm_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._srv_sock:
            try:
                self._srv_sock.close()
            except OSError:
                pass

        with self._clients_lock:
            for c in list(self._clients.keys()):
                try:
                    c.close()
                except OSError:
                    pass
            self._clients.clear()

    def _accept_loop(self) -> None:
        assert self._srv_sock is not None
        while not self._stop.is_set():
            try:
                c, addr = self._srv_sock.accept()
            except OSError:
                break

            c.settimeout(1.0)
            with self._clients_lock:
                self._clients[c] = ClientState(addr=addr)

            print(f"[fake_ardopc] client connected {addr}")
            threading.Thread(target=self._client_loop, args=(c,), daemon=True).start()

    def _drop_client(self, c: socket.socket) -> None:
        st = self._clients.get(c)
        if st:
            print(f"[fake_ardopc] client disconnected {st.addr}")
        try:
            c.close()
        except OSError:
            pass
        self._clients.pop(c, None)

    def send_to_all(self, payload: bytes) -> None:
        data = _frame(payload)
        dead: list[socket.socket] = []
        with self._clients_lock:
            for c, st in self._clients.items():
                try:
                    if self._should_drop():
                        continue

                    total_delay_s = self._compute_total_delay_s()
                    if total_delay_s <= 0.0:
                        c.sendall(data)
                        st.last_tx = time.time()
                    else:
                        # Schedule send to allow reordering between frames
                        send_at = time.time() + total_delay_s
                        with self._txq_lock:
                            heapq.heappush(self._txq, (send_at, c, data))
                            self._txq_wake.set()
                except OSError:
                    dead.append(c)

            for c in dead:
                self._drop_client(c)

    def send_to_one(self, c: socket.socket, payload: bytes) -> None:
        data = _frame(payload)
        with self._clients_lock:
            st = self._clients.get(c)
        if st is None:
            return
        try:
            if self._should_drop():
                return
            total_delay_s = self._compute_total_delay_s()
            if total_delay_s <= 0.0:
                c.sendall(data)
                st.last_tx = time.time()
            else:
                send_at = time.time() + total_delay_s
                with self._txq_lock:
                    heapq.heappush(self._txq, (send_at, c, data))
                    self._txq_wake.set()
        except OSError:
            with self._clients_lock:
                if c in self._clients:
                    self._drop_client(c)

    def _client_loop(self, c: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                chunk = c.recv(READ_CHUNK)
                if not chunk:
                    break
            except socket.timeout:
                continue
            except OSError:
                break

            with self._clients_lock:
                st = self._clients.get(c)
            if st is None:
                break

            st.rx_buf.extend(chunk)
            st.last_rx = time.time()

            while True:
                if len(st.rx_buf) < 2:
                    break
                frame_len = (st.rx_buf[0] << 8) | st.rx_buf[1]
                if frame_len > MAX_FRAME_LEN:
                    print(f"[fake_ardopc] {st.addr} invalid frame_len={frame_len} -> drop client")
                    return
                if len(st.rx_buf) < 2 + frame_len:
                    break

                payload = bytes(st.rx_buf[2: 2 + frame_len])
                del st.rx_buf[: 2 + frame_len]

                print(f"[fake_ardopc] RX from {st.addr}: {len(payload)} bytes: {_hex(payload)}")

                sync_desc = _try_decode_sync_request(payload)
                if sync_desc is not None:
                    print(f"[fake_ardopc] {sync_desc}")

                if self._echo:
                    self.send_to_one(c, payload)
                if self._broadcast:
                    self.send_to_all(payload)

        with self._clients_lock:
            if c in self._clients:
                self._drop_client(c)

    def _fake_ogm_loop(self) -> None:
        time.sleep(0.5)
        while not self._stop.is_set():
            payload = build_fake_ogm(
                origin=self._fake_ogm_id,
                seqno=self._fake_ogm_seqno,
                ttl=self._fake_ogm_ttl,
                link_metric=self._fake_ogm_metric,
            )
            self._fake_ogm_seqno = (self._fake_ogm_seqno + 1) & 0xFFFFFFFF
            print(f"[fake_ardopc] INJECT OGM: {_hex(payload)}")
            self.send_to_all(payload)

            remaining = self._fake_ogm_interval_s
            while remaining > 0 and not self._stop.is_set():
                step = 0.25 if remaining > 0.25 else remaining
                time.sleep(step)
                remaining -= step

    def run_stdin_injector(self) -> None:
        print("[fake_ardopc] stdin injector enabled. Enter hex payloads to inject. Ctrl+C to exit.")
        try:
            while not self._stop.is_set():
                line = input().strip()
                # Command mode: keep hex injection behavior, but support a few helpers.
                # - burst <origin> <dest> <channel> <count> <text...>
                #   Sends <count> CHAT_TYPE_MESSAGE frames with incrementing seqnos.
                # - sync_range <origin> <dest> <channel> <target_origin> <start> <end>
                #   Sends a CHAT_TYPE_SYNC_REQUEST with text JSON {"mode":"range",...}
                if line.startswith('burst '):
                    parts = line.split(' ', 5)
                    if len(parts) < 6:
                        print('[fake_ardopc] usage: burst <origin> <dest> <channel> <count> <text...>')
                        continue
                    _, origin, dest, channel, count_s, text_prefix = parts
                    try:
                        count = int(count_s)
                    except ValueError:
                        print('[fake_ardopc] invalid count')
                        continue
                    if count <= 0:
                        print('[fake_ardopc] count must be > 0')
                        continue
                    # Generate a per-origin seqno stream for scripted injection.
                    if not hasattr(self, '_inject_seqnos'):
                        setattr(self, '_inject_seqnos', {})
                    inject_seqnos = getattr(self, '_inject_seqnos')
                    origin8 = _ascii8(origin)
                    seqno = inject_seqnos.get(origin8, 1)
                    for i_msg in range(count):
                        msg = ChatMessage(
                            msg_type=CHAT_TYPE_MESSAGE,
                            channel=channel,
                            nick=origin,
                            text=f"{text_prefix} {i_msg + 1}/{count}",
                            created_ts=int(time.time()),
                        )
                        app = encode_chat_message(msg)
                        mesh = build_fake_data(origin=origin, dest=dest, seqno=seqno, ttl=5, app_payload=app,
                                               compress=True)
                        self.send_to_all(mesh)
                        seqno = (seqno + 1) & 0xFFFFFFFF
                    inject_seqnos[origin8] = seqno
                    print(f"[fake_ardopc] BURST sent {count} msg(s) origin={origin} dest={dest} channel={channel}")
                    continue

                if line.startswith('sync_range '):
                    parts = line.split(' ')
                    if len(parts) != 7:
                        print('[fake_ardopc] usage: sync_range <origin> <dest> <channel> <target_origin> <start> <end>')
                        continue
                    _, origin, dest, channel, target_origin_s, start_s, end_s = parts
                    try:
                        start = int(start_s)
                        end = int(end_s)
                    except ValueError:
                        print('[fake_ardopc] invalid start/end')
                        continue
                    if start < 0 or end < 0 or end < start:
                        print('[fake_ardopc] invalid range')
                        continue

                    # target_origin may be provided as 16-hex or as an ASCII node id.
                    target_origin_s = target_origin_s.strip()
                    target_hex: Optional[str] = None
                    if len(target_origin_s) == 16:
                        try:
                            _ = binascii.unhexlify(target_origin_s)
                            target_hex = target_origin_s.lower()
                        except (binascii.Error, ValueError):
                            target_hex = None
                    if target_hex is None:
                        target_hex = binascii.hexlify(_ascii8(target_origin_s)).decode('ascii')

                    payload = {
                        "mode": "range",
                        "origin_id_hex": target_hex,
                        "start": start,
                        "end": end,
                    }
                    msg = ChatMessage(
                        msg_type=CHAT_TYPE_SYNC_REQUEST,
                        channel=channel,
                        nick=origin,
                        text=json.dumps(payload),
                        created_ts=int(time.time()),
                    )
                    app = encode_chat_message(msg)

                    if not hasattr(self, '_inject_seqnos'):
                        setattr(self, '_inject_seqnos', {})
                    inject_seqnos = getattr(self, '_inject_seqnos')
                    origin8 = _ascii8(origin)
                    seqno = inject_seqnos.get(origin8, 1)

                    mesh = build_fake_data(origin=origin, dest=dest, seqno=seqno, ttl=5, app_payload=app, compress=True)
                    self.send_to_all(mesh)
                    inject_seqnos[origin8] = (seqno + 1) & 0xFFFFFFFF
                    print(
                        f"[fake_ardopc] SYNC_RANGE sent origin={origin} dest={dest} channel={channel} target={target_hex} seq={start}-{end}"
                    )
                    continue

                if not line:
                    continue
                try:
                    payload = _unhex(line)
                except (binascii.Error, ValueError) as e:
                    print(f"[fake_ardopc] invalid hex: {e}")
                    continue
                print(f"[fake_ardopc] INJECT: {len(payload)} bytes: {_hex(payload)}")
                self.send_to_all(payload)
        except (KeyboardInterrupt, EOFError):
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8515)
    ap.add_argument("--echo", action="store_true", help="echo received frames back to the same client")
    ap.add_argument("--broadcast", action="store_true", help="broadcast received frames to all clients")
    ap.add_argument("--stdin-inject", action="store_true", help="enable interactive hex injection via stdin")

    ap.add_argument("--drop-rate", type=float, default=0.0, help="drop outgoing frames with this probability (0.0-1.0)")
    ap.add_argument("--delay-ms", type=int, default=0, help="base delay added before every outgoing frame send")
    ap.add_argument("--jitter-ms", type=int, default=0,
                    help="additional random delay (0..jitter-ms) per outgoing frame")

    ap.add_argument("--reorder-rate", type=float, default=0.0,
                    help="probability to add extra delay to a frame to induce reordering (0.0-1.0)")
    ap.add_argument("--reorder-max-delay-ms", type=int, default=0,
                    help="maximum extra delay (0..N ms) applied when reorder triggers")

    ap.add_argument("--fake-ogm", action="store_true", help="periodically inject a fake OGM (mesh_node-compatible)")
    ap.add_argument("--fake-ogm-id", default="NOCALL-1",
                    help="fake node ID (ASCII; will be padded/truncated to 8 bytes)")
    ap.add_argument("--fake-ogm-interval", type=float, default=5.0, help="seconds between fake OGM broadcasts")
    ap.add_argument("--fake-ogm-ttl", type=int, default=5, help="TTL for fake OGM packet (0-255)")
    ap.add_argument("--fake-ogm-metric", type=int, default=255, help="link metric byte for fake OGM (0-255)")

    args = ap.parse_args()

    srv = FakeArdopServer(
        args.host,
        args.port,
        echo=args.echo,
        broadcast=args.broadcast,
        drop_rate=args.drop_rate,
        base_delay_ms=args.delay_ms,
        jitter_ms=args.jitter_ms,
        reorder_rate=args.reorder_rate,
        reorder_max_delay_ms=args.reorder_max_delay_ms,
        fake_ogm=args.fake_ogm,
        fake_ogm_id=args.fake_ogm_id,
        fake_ogm_interval_s=args.fake_ogm_interval,
        fake_ogm_ttl=args.fake_ogm_ttl,
        fake_ogm_metric=args.fake_ogm_metric,
    )
    srv.start()

    try:
        if args.stdin_inject:
            srv.run_stdin_injector()
        else:
            while True:
                time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        print("[fake_ardopc] stopping")
        srv.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
