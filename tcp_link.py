"""
tcp_link.py

TCP mesh link-layer wrapper over a TCP stream.

Goals:
- Act as a LinkClient compatible with MeshNode (start/stop/send).
- Provide BOTH:
  - server mode: accept inbound TCP mesh clients
  - client mode: connect to remote TCP mesh servers
- Exchange raw mesh frames using the SAME framing as ardop_link.py:
      uint16_be_len + frame_bytes
- Authenticate before frames flow using a link-local handshake:
      client -> server: b"QMESH1" + uint16_be_pw_len + pw_bytes
  If password mismatch, the server closes the connection.
- No mesh protocol changes. No invented fields. No broad exceptions.

Notes:
- Server accepts ONE connection at a time (keeps behavior simple and predictable).
- If the connection drops, server continues accepting.
- Client reconnects with bounded exponential backoff.
"""

from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

LOG = logging.getLogger(__name__)

_HANDSHAKE_MAGIC = b"QMESH1"

_HS_OK = b"\x00"
_HS_BAD_PW = b"\x01"
_HS_BAD_HANDSHAKE = b"\x02"


class TcpLinkError(Exception):
    pass


@dataclass(frozen=True)
class TcpClientConfig:
    host: str
    port: int
    password: str
    reconnect_base_delay: float = 5.0
    reconnect_max_delay: float = 60.0
    tx_queue_size: int = 1000


@dataclass(frozen=True)
class TcpServerConfig:
    port: int
    server_pw: str
    tx_queue_size: int = 1000


class TcpLinkClient:
    """
    LinkClient-compatible TCP mesh link.

    Use:
      TcpLinkClient.client(...)
      TcpLinkClient.server(...)
    """

    def __init__(
            self,
            *,
            rx_callback: Callable[[bytes], None],
            name: str,
            client_cfg: Optional[TcpClientConfig] = None,
            server_cfg: Optional[TcpServerConfig] = None,
    ) -> None:
        if (client_cfg is None) == (server_cfg is None):
            raise ValueError("Exactly one of client_cfg or server_cfg must be set")

        self._rx_callback = rx_callback
        self._name = name

        self._client_cfg = client_cfg
        self._server_cfg = server_cfg

        self._sock: Optional[socket.socket] = None
        self._srv_sock: Optional[socket.socket] = None

        self._running = threading.Event()
        self._connected = threading.Event()

        self._rx_thread: Optional[threading.Thread] = None
        self._tx_thread: Optional[threading.Thread] = None

        tx_qsz = 1000
        if self._client_cfg is not None:
            tx_qsz = int(self._client_cfg.tx_queue_size)
        if self._server_cfg is not None:
            tx_qsz = int(self._server_cfg.tx_queue_size)

        self._tx_queue: queue.Queue[bytes] = queue.Queue(maxsize=tx_qsz)
        self._rx_buffer = bytearray()
        self._lock = threading.Lock()

    @classmethod
    def client(
            cls,
            *,
            host: str,
            port: int,
            password: str,
            rx_callback: Callable[[bytes], None],
            reconnect_base_delay: float = 5.0,
            reconnect_max_delay: float = 60.0,
            tx_queue_size: int = 1000,
            name: str = "tcp-client-link",
    ) -> TcpLinkClient:
        return cls(
            rx_callback=rx_callback,
            name=name,
            client_cfg=TcpClientConfig(
                host=str(host),
                port=int(port),
                password=str(password),
                reconnect_base_delay=float(reconnect_base_delay),
                reconnect_max_delay=float(reconnect_max_delay),
                tx_queue_size=int(tx_queue_size),
            ),
        )

    @classmethod
    def server(
            cls,
            *,
            port: int,
            server_pw: str,
            rx_callback: Callable[[bytes], None],
            tx_queue_size: int = 1000,
            name: str = "tcp-server-link",
    ) -> TcpLinkClient:
        return cls(
            rx_callback=rx_callback,
            name=name,
            server_cfg=TcpServerConfig(
                port=int(port),
                server_pw=str(server_pw),
                tx_queue_size=int(tx_queue_size),
            ),
        )

    # ------------------------------------------------------------------
    # LinkClient interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running.is_set():
            return
        self._running.set()

        if self._server_cfg is not None:
            LOG.info("%s starting in SERVER mode on port %d", self._name, self._server_cfg.port)
        if self._client_cfg is not None:
            LOG.info("%s starting in CLIENT mode to %s:%d", self._name, self._client_cfg.host, self._client_cfg.port)

        self._rx_thread = threading.Thread(
            target=self._rx_loop,
            name=f"{self._name}-rx",
            daemon=True,
        )
        self._tx_thread = threading.Thread(
            target=self._tx_loop,
            name=f"{self._name}-tx",
            daemon=True,
        )
        self._rx_thread.start()
        self._tx_thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if not self._running.is_set():
            return

        self._running.clear()

        try:
            self._tx_queue.put_nowait(b"")
        except queue.Full:
            pass

        if self._rx_thread is not None:
            self._rx_thread.join(timeout=timeout)
        if self._tx_thread is not None:
            self._tx_thread.join(timeout=timeout)

        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    LOG.warning("Error closing TCP link socket", exc_info=True)
                self._sock = None
            if self._srv_sock is not None:
                try:
                    self._srv_sock.close()
                except OSError:
                    LOG.warning("Error closing TCP server socket", exc_info=True)
                self._srv_sock = None
            self._connected.clear()

    def send(self, payload: bytes) -> None:
        if not self._running.is_set():
            return
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError("payload must be bytes-like")
        if payload == b"":
            return
        try:
            self._tx_queue.put_nowait(bytes(payload))
        except queue.Full:
            # Drop rather than block the mesh node
            return

    # ------------------------------------------------------------------
    # Connection management + handshake
    # ------------------------------------------------------------------

    def _connect_client_with_backoff(self) -> None:
        assert self._client_cfg is not None
        delay = self._client_cfg.reconnect_base_delay
        if delay <= 0.0:
            delay = 0.2

        while self._running.is_set() and not self._connected.is_set():
            try:
                LOG.info("%s connecting to %s:%d", self._name, self._client_cfg.host, self._client_cfg.port)
                sock = socket.create_connection((self._client_cfg.host, self._client_cfg.port), timeout=10.0)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(5.0)

                self._client_handshake(sock, self._client_cfg.password)

                with self._lock:
                    if self._sock is not None:
                        try:
                            self._sock.close()
                        except OSError:
                            pass
                    self._sock = sock
                    self._rx_buffer.clear()
                    self._connected.set()
                LOG.info("%s connected to %s:%d", self._name, self._client_cfg.host, self._client_cfg.port)
                return
            except TcpLinkError as exc:
                # Bad password / handshake: do not retry forever.
                LOG.error("%s handshake failed: %s (stopping)", self._name, exc)
                self.stop()
                return
            except OSError as exc:
                LOG.warning("%s connect failed (%s); retrying in %.1fs", self._name, exc, delay)
                time.sleep(delay)
                delay *= 2.0
                if delay > self._client_cfg.reconnect_max_delay:
                    delay = self._client_cfg.reconnect_max_delay

    def _ensure_server_listener(self) -> None:
        assert self._server_cfg is not None
        with self._lock:
            if self._srv_sock is not None:
                return
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("", self._server_cfg.port))
            srv.listen(1)
            srv.settimeout(2.0)
            self._srv_sock = srv
            LOG.info("%s listening on 0.0.0.0:%d", self._name, self._server_cfg.port)

    def _accept_server(self) -> None:
        assert self._server_cfg is not None
        self._ensure_server_listener()
        assert self._srv_sock is not None

        while self._running.is_set() and not self._connected.is_set():
            try:
                conn, addr = self._srv_sock.accept()
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.settimeout(5.0)

                if not self._server_handshake(conn, self._server_cfg.server_pw):
                    LOG.warning("%s rejected connection (bad password or handshake)", self._name)
                    try:
                        conn.close()
                    except OSError:
                        pass
                    # Avoid busy-loop / flood amplification on repeated bad attempts
                    time.sleep(0.25)
                    continue

                with self._lock:
                    if self._sock is not None:
                        try:
                            self._sock.close()
                        except OSError:
                            pass
                    self._sock = conn
                    self._rx_buffer.clear()
                    self._connected.set()
                LOG.info("%s accepted %s", self._name, addr)
                return
            except socket.timeout:
                continue
            except OSError:
                return

    @staticmethod
    def _client_handshake(sock: socket.socket, password: str) -> None:
        pw_bytes = password.encode("utf-8")
        pw_len = len(pw_bytes)
        if pw_len > 65535:
            raise TcpLinkError("Password too long")
        hdr = _HANDSHAKE_MAGIC + pw_len.to_bytes(2, "big")
        sock.sendall(hdr + pw_bytes)

        # Wait for server response (1 byte)
        resp = TcpLinkClient._recv_exact(sock, 1)
        if resp == _HS_OK:
            return
        if resp == _HS_BAD_PW:
            raise TcpLinkError("Server rejected password")
        raise TcpLinkError("Server rejected handshake")

    @staticmethod
    def _server_handshake(sock: socket.socket, expected_pw: str) -> bool:
        try:
            header = TcpLinkClient._recv_exact(sock, 8)
        except OSError:
            return False

        if header[:6] != _HANDSHAKE_MAGIC:
            try:
                sock.sendall(_HS_BAD_HANDSHAKE)
            except OSError:
                pass
            return False

        pw_len = int.from_bytes(header[6:8], "big")
        if pw_len < 0 or pw_len > 65535:
            try:
                sock.sendall(_HS_BAD_HANDSHAKE)
            except OSError:
                pass
            return False

        try:
            pw = TcpLinkClient._recv_exact(sock, pw_len)
        except OSError:
            return False

        if pw != expected_pw.encode("utf-8"):
            try:
                sock.sendall(_HS_BAD_PW)
            except OSError:
                pass
            return False

        try:
            sock.sendall(_HS_OK)
        except OSError:
            return False
        return True

    # ------------------------------------------------------------------
    # RX/TX loops + framing
    # ------------------------------------------------------------------

    def _rx_loop(self) -> None:
        while self._running.is_set():
            if not self._connected.is_set():
                try:
                    if self._server_cfg is not None:
                        self._accept_server()
                    else:
                        self._connect_client_with_backoff()
                except (OSError, TcpLinkError):
                    time.sleep(0.2)
                    continue

            with self._lock:
                sock = self._sock

            if sock is None:
                self._connected.clear()
                continue

            try:
                chunk = sock.recv(4096)
                if not chunk:
                    raise OSError("socket closed")
                self._rx_buffer.extend(chunk)
                self._drain_rx_buffer()
            except socket.timeout:
                continue
            except OSError:
                self._drop_connection()

    def _drain_rx_buffer(self) -> None:
        while True:
            if len(self._rx_buffer) < 2:
                return
            frame_len = (self._rx_buffer[0] << 8) | self._rx_buffer[1]
            if frame_len <= 0:
                del self._rx_buffer[:2]
                continue
            if len(self._rx_buffer) < 2 + frame_len:
                return

            frame = bytes(self._rx_buffer[2:2 + frame_len])
            del self._rx_buffer[:2 + frame_len]

            try:
                self._rx_callback(frame)
            except (ValueError, OSError):
                continue

    def _tx_loop(self) -> None:
        while self._running.is_set():
            try:
                payload = self._tx_queue.get(timeout=0.25)
            except queue.Empty:
                continue

            if payload == b"":
                continue

            if not self._connected.is_set():
                continue

            with self._lock:
                sock = self._sock

            if sock is None:
                self._connected.clear()
                continue

            try:
                if len(payload) > 65535:
                    continue
                sock.sendall(len(payload).to_bytes(2, "big") + payload)
            except OSError:
                self._drop_connection()

    def _drop_connection(self) -> None:
        LOG.warning("%s connection dropped", self._name)
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
            self._connected.clear()

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise OSError("socket closed")
            buf += chunk
        return buf
