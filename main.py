# main.py (example usage — do NOT call anything at top level)

import logging
from mesh_config import (
    MeshNodeConfig,
    KISSConnectionConfig,
    MeshSecurityConfig,
    MeshRoutingConfig,
    TransportType,
)
from kiss_link import KISSClient
from mesh_node import MeshNode

logging.basicConfig(level=logging.INFO)


def build_kiss_client(rx_callback):
    kiss_cfg = KISSConnectionConfig(
        transport=TransportType.TCP,
        tcp_host="127.0.0.1",
        tcp_port=8001,
    )
    return KISSClient(kiss_cfg, rx_callback)


def create_mesh_node() -> MeshNode:
    node_cfg = MeshNodeConfig(
        callsign="KD9YQK-1",
        mesh_dest_callsign="QMESH-0",
        kiss_config=None,  # we use factory instead
        routing_config=MeshRoutingConfig(),
        security_config=MeshSecurityConfig(
            enable_encryption=False,  # keep disabled by default
            key=None,
        ),
    )
    mesh_node = MeshNode(node_cfg, kiss_client_factory=build_kiss_client)
    return mesh_node


def run_node() -> None:
    node = create_mesh_node()
    node.start()
    # Your event loop or long-running logic goes here.
    # Make sure to call node.stop() on shutdown.


if __name__ == "__main__":
    run_node()
