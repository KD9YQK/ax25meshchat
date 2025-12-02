"""
Configuration and enums for the VHF mesh stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class TransportType(str, Enum):
    SERIAL = "serial"
    TCP = "tcp"


@dataclass
class KISSConnectionConfig:
    """
    Configuration for a KISS3 connection.
    """

    transport: TransportType

    # Serial configuration
    serial_port: str = "/dev/ttyUSB0"
    serial_baud: int = 1200

    # TCP configuration (e.g. Direwolf KISS TCP)
    tcp_host: str = "127.0.0.1"
    tcp_port: int = 8001

    # Reconnect behavior
    reconnect_base_delay: float = 5.0
    reconnect_max_delay: float = 60.0

    # TX queue
    tx_queue_size: int = 1000


@dataclass
class MeshSecurityConfig:
    """
    Optional security settings.

    NOTE: Enabling encryption on amateur bands may violate local regulations.
    Ensure you understand and comply with your jurisdiction's rules.
    """

    enable_encryption: bool = False
    # 32 bytes for AES-256 (if using the default AES-GCM implementation).
    key: Optional[bytes] = None


@dataclass
class MeshRoutingConfig:
    """
    Routing / BATMAN-lite behavior.
    """

    ogm_interval_seconds: float = 10.0
    ogm_ttl: int = 5
    route_expiry_seconds: float = 120.0
    neighbor_expiry_seconds: float = 60.0
    data_seen_expiry_seconds: float = 30.0


@dataclass
class MeshNodeConfig:
    """
    Overall node configuration.
    """

    callsign: str
    mesh_dest_callsign: str = "QMESH-0"
    kiss_config: KISSConnectionConfig | None = None
    routing_config: MeshRoutingConfig = MeshRoutingConfig()
    security_config: MeshSecurityConfig = MeshSecurityConfig()
