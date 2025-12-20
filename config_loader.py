# config_loader.py
#
# YAML → in-memory config structs for the mesh + chat stack.
#
# This version is ARDOP-only at the link layer. All KISS/AX.25 options
# have been removed in favor of a simple ARDOP TCP connection config.

from __future__ import annotations

from typing import Dict, Any
import binascii

import yaml  # pip install pyyaml

from mesh_config import (
    ArdopConnectionConfig,
    MeshRoutingConfig,
    MeshSecurityConfig,
    MeshNodeConfig,
)
from chat_client import MeshChatConfig, ChatPeer  # if you're using chat


def _get_required(mapping: Dict[str, Any], key: str) -> Any:
    if key not in mapping:
        raise KeyError(f"Missing required config key: {key}")
    return mapping[key]


# ---------------------------------------------------------------------------
# ARDOP link config
# ---------------------------------------------------------------------------

def load_ardop_config(root: Dict[str, Any]) -> ArdopConnectionConfig:
    """Load ARDOP TCP link configuration from top-level `ardop` section.

    Example YAML:

        ardop:
          host: "127.0.0.1"
          port: 8515
          reconnect_base_delay: 5.0
          reconnect_max_delay: 60.0
          tx_queue_size: 1000
    """

    ardop_cfg_any = root.get("ardop", {})
    if not isinstance(ardop_cfg_any, dict):
        ardop_cfg = {}
    else:
        ardop_cfg = ardop_cfg_any

    host = str(ardop_cfg.get("host", "127.0.0.1"))
    port = int(ardop_cfg.get("port", 8515))

    reconnect_base_delay = float(ardop_cfg.get("reconnect_base_delay", 5.0))
    reconnect_max_delay = float(ardop_cfg.get("reconnect_max_delay", 60.0))
    tx_queue_size = int(ardop_cfg.get("tx_queue_size", 1000))

    return ArdopConnectionConfig(
        host=host,
        port=port,
        reconnect_base_delay=reconnect_base_delay,
        reconnect_max_delay=reconnect_max_delay,
        tx_queue_size=tx_queue_size,
    )


# ---------------------------------------------------------------------------
# Mesh routing / security / node config
# ---------------------------------------------------------------------------

def load_routing_config(root: Dict[str, Any]) -> MeshRoutingConfig:
    routing_cfg_any = root.get("routing", {})
    if not isinstance(routing_cfg_any, dict):
        routing_cfg = {}
    else:
        routing_cfg = routing_cfg_any

    ogm_interval = float(routing_cfg.get("ogm_interval_seconds", 600.0))
    ogm_ttl = int(routing_cfg.get("ogm_ttl", 5))
    route_expiry = float(routing_cfg.get("route_expiry_seconds", 120.0))
    neighbor_expiry = float(routing_cfg.get("neighbor_expiry_seconds", 60.0))
    data_seen_expiry = float(routing_cfg.get("data_seen_expiry_seconds", 30.0))

    return MeshRoutingConfig(
        ogm_interval_seconds=ogm_interval,
        ogm_ttl=ogm_ttl,
        route_expiry_seconds=route_expiry,
        neighbor_expiry_seconds=neighbor_expiry,
        data_seen_expiry_seconds=data_seen_expiry,
    )


def load_security_config(root: Dict[str, Any]) -> MeshSecurityConfig:
    sec_cfg_any = root.get("security", {})
    if not isinstance(sec_cfg_any, dict):
        sec_cfg = {}
    else:
        sec_cfg = sec_cfg_any

    enable_encryption = bool(sec_cfg.get("enable_encryption", False))
    key_hex = sec_cfg.get("key_hex")
    key_bytes = None
    if key_hex is not None:
        key_bytes = binascii.unhexlify(key_hex)

    return MeshSecurityConfig(
        enable_encryption=enable_encryption,
        key=key_bytes,
    )


def load_mesh_node_config(root: Dict[str, Any]) -> MeshNodeConfig:
    """Load MeshNodeConfig from the top-level `mesh`, `ardop`, etc. sections."""

    mesh_cfg_any = root.get("mesh", {})
    if not isinstance(mesh_cfg_any, dict):
        mesh_cfg = {}
    else:
        mesh_cfg = mesh_cfg_any

    callsign = _get_required(mesh_cfg, "callsign")
    mesh_dest_callsign = mesh_cfg.get("mesh_dest_callsign", "QMESH-0")

    ardop_cfg = load_ardop_config(root)
    routing_cfg = load_routing_config(root)
    security_cfg = load_security_config(root)

    return MeshNodeConfig(
        callsign=callsign,
        mesh_dest_callsign=mesh_dest_callsign,
        ardop_config=ardop_cfg,
        routing_config=routing_cfg,
        security_config=security_cfg,
    )


# ---------------------------------------------------------------------------
# Chat config
# ---------------------------------------------------------------------------

def load_chat_config_from_yaml(path: str) -> MeshChatConfig:
    """Load complete MeshChatConfig (MeshNodeConfig + chat) from YAML file."""

    with open(path, "r", encoding="utf-8") as f:
        root = yaml.safe_load(f)

    if not isinstance(root, dict):
        raise ValueError("Top-level YAML must be a mapping")

    mesh_node_cfg = load_mesh_node_config(root)

    chat_cfg_any = root.get("chat", {})
    if not isinstance(chat_cfg_any, dict):
        chat_cfg_raw = {}
    else:
        chat_cfg_raw = chat_cfg_any

    db_path = str(_get_required(chat_cfg_raw, "db_path"))

    # ---- sync config (optional) ----
    sync_any = chat_cfg_raw.get("sync", {})
    if not isinstance(sync_any, dict):
        sync_raw: Dict[str, Any] = {}
    else:
        sync_raw = sync_any

    sync_enabled = bool(sync_raw.get("enabled", True))
    sync_last_n_messages = int(sync_raw.get("last_n_messages", 200))
    sync_max_send_per_response = int(sync_raw.get("max_send_per_response", 200))
    sync_auto_sync_on_new_peer = bool(sync_raw.get("auto_sync_on_new_peer", True))
    sync_min_sync_interval_seconds = float(sync_raw.get("min_sync_interval_seconds", 30.0))

    if sync_last_n_messages < 1:
        raise ValueError("chat.sync.last_n_messages must be >= 1")
    if sync_max_send_per_response < 1:
        raise ValueError("chat.sync.max_send_per_response must be >= 1")
    if sync_min_sync_interval_seconds < 0.0:
        raise ValueError("chat.sync.min_sync_interval_seconds must be >= 0")
    # -------------------------------

    peers_raw_any = chat_cfg_raw.get("peers", {})
    if not isinstance(peers_raw_any, dict):
        peers_raw = {}
    else:
        peers_raw = peers_raw_any

    peers: Dict[str, ChatPeer] = {}

    for nickname, peer_data_any in peers_raw.items():
        if not isinstance(peer_data_any, dict):
            continue

        node_id_hex = _get_required(peer_data_any, "node_id_hex")
        peer_nick = str(peer_data_any.get("nick", nickname))

        node_id_bytes = binascii.unhexlify(node_id_hex)
        if len(node_id_bytes) != 8:
            raise ValueError(
                f"node_id_hex for peer {nickname} must decode to 8 bytes"
            )

        peers[nickname] = ChatPeer(
            node_id=node_id_bytes,
            nick=peer_nick,
        )

    return MeshChatConfig(
        mesh_node_config=mesh_node_cfg,
        db_path=db_path,
        peers=peers,
        sync_enabled=sync_enabled,
        sync_last_n_messages=sync_last_n_messages,
        sync_max_send_per_response=sync_max_send_per_response,
        sync_auto_sync_on_new_peer=sync_auto_sync_on_new_peer,
        sync_min_sync_interval_seconds=sync_min_sync_interval_seconds,
    )
