"""
Configuration and enums for the VHF mesh stack.

This project is now ARDOP-only at the link layer. All KISS/AX.25
configuration has been removed in favor of a simple ARDOP TCP
connection config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class ArdopConnectionConfig:
    """Configuration for the ARDOP TCP link.

    This describes how the mesh node connects to the ARDOP TNC or modem
    process that exposes a TCP byte-stream interface.

    - host: hostname or IP of the ARDOP TNC
    - port: TCP port for the TNC data stream
    - reconnect_base_delay: initial reconnect backoff, in seconds
    - reconnect_max_delay: maximum reconnect backoff, in seconds
    - tx_queue_size: maximum number of queued frames waiting to be sent
    """

    host: str = "127.0.0.1"
    port: int = 8515

    reconnect_base_delay: float = 5.0
    reconnect_max_delay: float = 60.0

    tx_queue_size: int = 1000


@dataclass
class MeshRoutingConfig:
    """Routing and neighbor behavior."""

    ogm_interval_seconds: float = 600.0  # how often to send OGMs
    ogm_ttl: int = 5  # hop limit for OGMs
    route_expiry_seconds: float = 120.0
    neighbor_expiry_seconds: float = 60.0
    data_seen_expiry_seconds: float = 30.0


@dataclass
class MeshSecurityConfig:
    """Security / crypto configuration.

    NOTE: On amateur radio you must keep `enable_encryption` false.
    """

    enable_encryption: bool = False
    key: Optional[bytes] = None


@dataclass
class TcpMeshServerConfig:
    enabled: bool = False
    server_pw: str = ""
    server_port: int = 9000


@dataclass
class TcpMeshLinkConfig:
    name: str
    enabled: bool
    host: str
    port: int
    password: str
    reconnect_base_delay: float = 5.0
    reconnect_max_delay: float = 60.0
    tx_queue_size: int = 1000


@dataclass
class TcpMeshConfig:
    server: TcpMeshServerConfig = TcpMeshServerConfig()
    links: List[TcpMeshLinkConfig] = field(default_factory=list)


@dataclass
class MeshNodeConfig:
    """Overall node configuration."""

    callsign: str
    mesh_dest_callsign: str = "QMESH-0"

    # ARDOP link-layer connection settings
    ardop_config: Optional[ArdopConnectionConfig] = None

    # Optional TCP mesh links (server and/or client links)
    tcp_mesh: Optional[TcpMeshConfig] = None
    routing_config: MeshRoutingConfig = MeshRoutingConfig()
    security_config: MeshSecurityConfig = MeshSecurityConfig()
