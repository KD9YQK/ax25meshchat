"""
ARDOP link-layer wrapper over a TCP stream.

Provides:
- Background RX/TX threads
- Reconnect with backoff
- Simple `send(payload_bytes)` API
- A receive callback that yields *raw mesh payload bytes*.

This module is intentionally generic: it treats the TCP connection as a
reliable full-duplex byte stream (e.g. an ARDOP TNC data port).
On the wire between host and link layer we use a simple 2-byte big-endian
length prefix per frame:

    [len_hi][len_lo] + <raw mesh payload bytes>

The *payload* seen by `rx_callback` is the raw mesh payload (mesh header +
body) with no AX.25 or KISS framing.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import logging
import queue
import socket
import threading
import time
from typing import Callable, Optional

from mesh_config import ArdopConnectionConfig


LOG = logging.getLogger(__name__)


@dataclass
class LinkMetrics:
    name: str
    link_type: str
    running: bool
    connected: bool

    started_ts: float = 0.0
    last_connect_ts: float = 0.0
    last_disconnect_ts: float = 0.0
    last_rx_ts: float = 0.0
    last_tx_ts: float = 0.0

    rx_frames: int = 0
    tx_frames: int = 0
    rx_bytes: int = 0
    tx_bytes: int = 0

    connect_attempts: int = 0
    connect_successes: int = 0
    disconnects: int = 0
    tx_dropped_no_conn: int = 0
    tx_errors: int = 0
    rx_errors: int = 0
    last_error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class ArdopLinkError(Exception):
    """Base exception for ArdopLinkClient."""


class ArdopLinkClient:
    """TCP-based link client for ARDOP (or any reliable modem byte pipe).

    Public API:
        start() -> None
        stop(timeout: float = 5.0) -> None
        send(payload: bytes, block: bool = True, timeout: Optional[float] = None) -> None

    The user supplies an `rx_callback(frame: bytes)` that receives complete
    frames where each frame is a single "raw mesh payload" blob.

    Internally, frames are encoded on the TCP stream as:
        uint16_be_len + frame_bytes
    """

    def __init__(
        self,
        config: ArdopConnectionConfig,
        rx_callback: Callable[[bytes], None],
        name: str = "ardop-link",
    ) -> None:
        self._config = config
        self._rx_callback = rx_callback
        self._name = name

        self._sock: Optional[socket.socket] = None
        self._running = threading.Event()
        self._connected = threading.Event()

        self._rx_thread: Optional[threading.Thread] = None
        self._tx_thread: Optional[threading.Thread] = None

        # TX queue holds *payload* frames (no length prefix)
        self._tx_queue: "queue.Queue[bytes]" = queue.Queue(
            maxsize=self._config.tx_queue_size
        )

        # Buffer for assembling frames from the TCP stream
        self._rx_buffer = bytearray()

        self._lock = threading.Lock()

        self._metrics = LinkMetrics(
            name=str(self._name),
            link_type="ardop",
            running=False,
            connected=False,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start RX/TX threads and establish a TCP connection."""
        if self._running.is_set():
            LOG.warning("ArdopLinkClient %s already running", self._name)
            return

        self._running.set()
        self._metrics.running = True
        self._metrics.started_ts = time.time()

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
        """Stop RX/TX threads and close the TCP connection."""
        if not self._running.is_set():
            return

        self._running.clear()
        self._metrics.running = False

        # Wake TX thread so it can exit
        try:
            self._tx_queue.put_nowait(b"")
        except queue.Full:
            LOG.warning("TX queue full while stopping; forcing shutdown")

        if self._rx_thread is not None:
            self._rx_thread.join(timeout=timeout)
        if self._tx_thread is not None:
            self._tx_thread.join(timeout=timeout)

        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    LOG.warning("Error closing ARDOP socket", exc_info=True)
                self._sock = None
                self._connected.clear()
                self._metrics.connected = False
                self._metrics.last_disconnect_ts = time.time()
                self._metrics.disconnects += 1

    def send(
        self,
        payload: bytes,
        block: bool = True,
        timeout: Optional[float] = None,
    ) -> None:
        """Queue a single *mesh frame payload* for transmission.

        The payload is any bytes blob (mesh header + body). The client
        will length-prefix it on the TCP stream.

        Raises ArdopLinkError if the client is not running or the queue is full.
        """
        if not self._running.is_set():
            raise ArdopLinkError("Cannot send: client is not running")

        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError("payload must be bytes-like")

        try:
            # NOTE: we treat b"" in the queue as a shutdown sentinel.
            # Empty frames are unusual; if you really need them, this
            # logic could be adjusted to use a different sentinel.
            if payload == b"":
                LOG.debug("Ignoring empty payload send() request")
                return
            self._tx_queue.put(bytes(payload), block=block, timeout=timeout)
        except queue.Full as put_error:
            raise ArdopLinkError("TX queue is full") from put_error

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def get_metrics(self) -> dict:
        # Snapshot metrics (thread-safe enough for approximate diagnostics)
        with self._lock:
            self._metrics.connected = self._connected.is_set()
            self._metrics.running = self._running.is_set()
            return dict(self._metrics.to_dict())

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect_with_backoff(self) -> None:
        """Ensure there is a working TCP connection, with backoff on failure."""
        delay = self._config.reconnect_base_delay

        while self._running.is_set() and not self._connected.is_set():
            try:
                self._metrics.connect_attempts += 1
                LOG.info(
                    "Connecting ARDOP link to %s:%d",
                    self._config.host,
                    self._config.port,
                )
                sock = socket.create_connection(
                    (self._config.host, self._config.port),
                    timeout=10.0,
                )
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(5.0)

                with self._lock:
                    if self._sock is not None:
                        try:
                            self._sock.close()
                        except OSError:
                            LOG.warning(
                                "Error closing previous ARDOP socket",
                                exc_info=True,
                            )
                    self._sock = sock
                    self._rx_buffer.clear()
                    self._connected.set()
                    self._metrics.connected = True
                    self._metrics.last_connect_ts = time.time()
                    self._metrics.connect_successes += 1
                    self._metrics.last_error = ""

                LOG.info("ARDOP TCP connection established")
                return

            except OSError:
                self._connected.clear()
                self._metrics.last_error = "connect_failed"
                LOG.warning(
                    "ARDOP TCP connection failed; retrying in %.1f s",
                    delay,
                    exc_info=True,
                )
                time.sleep(delay)
                if delay < self._config.reconnect_max_delay:
                    delay *= 2.0

    # ------------------------------------------------------------------
    # RX / TX loops
    # ------------------------------------------------------------------

    def _rx_loop(self) -> None:
        """Receive loop: assemble framed payloads from the TCP stream."""
        while self._running.is_set():
            if (not self._connected.is_set()) or (self._sock is None):
                self._connect_with_backoff()
                if (not self._connected.is_set()) or (self._sock is None):
                    # Give up for a moment and retry
                    time.sleep(1.0)
                    continue

            try:
                with self._lock:
                    sock = self._sock
                if sock is None:
                    self._connected.clear()
                    continue

                data = sock.recv(4096)
                if not data:
                    # Remote closed connection
                    LOG.warning("ARDOP TCP connection closed by peer; reconnecting")
                    with self._lock:
                        try:
                            sock.close()
                        except OSError:
                            pass
                        self._sock = None
                        self._connected.clear()
                        self._metrics.connected = False
                        self._metrics.last_disconnect_ts = time.time()
                        self._metrics.disconnects += 1
                    time.sleep(1.0)
                    continue

                self._process_rx_bytes(data)

            except socket.timeout:
                # Normal; just loop again
                continue
            except OSError:
                LOG.warning("RX loop lost ARDOP connection; reconnecting", exc_info=True)
                with self._lock:
                    if self._sock is not None:
                        try:
                            self._sock.close()
                        except OSError:
                            pass
                        self._sock = None
                        self._connected.clear()
                        self._metrics.connected = False
                        self._metrics.last_disconnect_ts = time.time()
                        self._metrics.disconnects += 1
                time.sleep(1.0)

    def _tx_loop(self) -> None:
        """Transmit loop: length-prefix and send frames from the queue."""
        while self._running.is_set():
            try:
                payload = self._tx_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if not self._running.is_set():
                break
            if payload == b"":
                # Sentinel used during shutdown
                continue

            if (not self._connected.is_set()) or (self._sock is None):
                if (not self._connected.is_set()) or (self._sock is None):
                    self._metrics.tx_dropped_no_conn += 1
                    LOG.warning("Dropping TX frame: no ARDOP TCP connection available")
                    continue

            # Build [len_hi][len_lo] + payload
            if len(payload) > 0xFFFF:
                LOG.warning("Payload too large for 16-bit length; dropping frame")
                continue

            frame_len = len(payload)
            header = bytes([(frame_len >> 8) & 0xFF, frame_len & 0xFF])
            to_send = header + payload

            try:
                with self._lock:
                    sock = self._sock
                if sock is None:
                    raise ArdopLinkError("ARDOP socket missing in TX loop")

                total_sent = 0
                while total_sent < len(to_send):
                    sent = sock.send(to_send[total_sent:])
                    if sent == 0:
                        raise ArdopLinkError("Socket connection broken during send")
                    total_sent += sent

                # Successful send
                self._metrics.tx_frames += 1
                self._metrics.tx_bytes += int(frame_len)
                self._metrics.last_tx_ts = time.time()

            except (OSError, ArdopLinkError):
                self._metrics.tx_errors += 1
                self._metrics.last_error = "tx_error"
                LOG.warning(
                    "Error writing ARDOP frame; dropping connection and retrying",
                    exc_info=True,
                )
                with self._lock:
                    if self._sock is not None:
                        try:
                            self._sock.close()
                        except OSError:
                            pass
                        self._sock = None
                        self._connected.clear()
                time.sleep(1.0)

    # ------------------------------------------------------------------
    # RX framing
    # ------------------------------------------------------------------

    # noinspection PyBroadException
    def _process_rx_bytes(self, data: bytes) -> None:
        """Append incoming bytes to buffer and extract complete frames."""
        self._rx_buffer.extend(data)

        # Try to peel off as many frames as possible
        while True:
            if len(self._rx_buffer) < 2:
                # Need more bytes for length prefix
                return

            frame_len = (self._rx_buffer[0] << 8) | self._rx_buffer[1]
            if len(self._rx_buffer) < 2 + frame_len:
                # Incomplete frame; wait for more data
                return

            # Extract frame
            start = 2
            end = 2 + frame_len
            frame = bytes(self._rx_buffer[start:end])

            self._metrics.rx_frames += 1
            self._metrics.rx_bytes += int(frame_len)
            self._metrics.last_rx_ts = time.time()

            # Remove from buffer
            del self._rx_buffer[:end]

            # Deliver to user callback
            try:
                self._rx_callback(frame)
            except (ValueError, RuntimeError, ArdopLinkError):
                # Expected "bad frame" / "cannot decode" style failures from consumers.
                # We drop the frame and continue.
                self._metrics.rx_errors += 1
                self._metrics.last_error = "rx_callback_error"
                LOG.warning(
                    "Error in ARDOP RX callback; frame dropped",
                    exc_info=True,
                )
            except Exception:
                # Any other exception is treated as a programming error in the consumer.
                # Stop the link client rather than silently swallowing it in a tight loop.
                self._metrics.last_error = "rx_callback_crash"
                LOG.exception(
                    "Unhandled exception in ARDOP RX callback; stopping link client",
                )
                self._running.clear()
                self._connected.clear()
                with self._lock:
                    if self._sock is not None:
                        try:
                            self._sock.close()
                        except OSError:
                            pass
                        self._sock = None
                return
