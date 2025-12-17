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
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


MAX_FRAME_LEN = 65535
READ_CHUNK = 4096

MESH_VERSION = 0x01
MESH_MSG_OGM = 0x01


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

    def start(self) -> None:
        self._srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv_sock.bind((self._host, self._port))
        self._srv_sock.listen(10)
        print(f"[fake_ardopc] listening on {self._host}:{self._port}")
        threading.Thread(target=self._accept_loop, daemon=True).start()

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
        dead = []
        with self._clients_lock:
            for c, st in self._clients.items():
                try:
                    c.sendall(data)
                    st.last_tx = time.time()
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
            c.sendall(data)
            st.last_tx = time.time()
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

                payload = bytes(st.rx_buf[2 : 2 + frame_len])
                del st.rx_buf[: 2 + frame_len]

                print(f"[fake_ardopc] RX from {st.addr}: {len(payload)} bytes: {_hex(payload)}")

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

    ap.add_argument("--fake-ogm", action="store_true", help="periodically inject a fake OGM (mesh_node-compatible)")
    ap.add_argument("--fake-ogm-id", default="NOCALL-1", help="fake node ID (ASCII; will be padded/truncated to 8 bytes)")
    ap.add_argument("--fake-ogm-interval", type=float, default=5.0, help="seconds between fake OGM broadcasts")
    ap.add_argument("--fake-ogm-ttl", type=int, default=5, help="TTL for fake OGM packet (0-255)")
    ap.add_argument("--fake-ogm-metric", type=int, default=255, help="link metric byte for fake OGM (0-255)")

    args = ap.parse_args()

    srv = FakeArdopServer(
        args.host,
        args.port,
        echo=args.echo,
        broadcast=args.broadcast,
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
