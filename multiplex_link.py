"""
multiplex_link.py

MultiplexLinkClient: a LinkClient that fans out sends to multiple underlying
LinkClients. RX frames are already delivered to MeshNode via the shared
rx_callback each LinkClient was constructed with.

This keeps MeshNode unchanged while allowing multi-link operation.
"""

from __future__ import annotations

from typing import List

from mesh_node import LinkClient


class MultiplexLinkClient:
    def __init__(self, links: List[LinkClient]) -> None:
        self._links = list(links)

    def start(self) -> None:
        for link in self._links:
            link.start()

    def stop(self) -> None:
        for link in self._links:
            try:
                link.stop()  # type: ignore[misc]
            except TypeError:
                link.stop()

    def send(self, payload: bytes) -> None:
        for link in self._links:
            link.send(payload)

    def get_metrics(self) -> dict:
        # Aggregate metrics from underlying links if available.
        items = []
        any_connected = False
        any_running = False
        for link in self._links:
            gm = getattr(link, "get_metrics", None)
            if callable(gm):
                m = gm()
                items.append(m)
                if bool(m.get("connected")):
                    any_connected = True
                if bool(m.get("running")):
                    any_running = True
            else:
                # Minimal fallback
                items.append({"name": getattr(link, "_name", "link"), "connected": False, "running": True})

        return {
            "name": "multiplex",
            "link_type": "multiplex",
            "running": any_running,
            "connected": any_connected,
            "links": items,
        }

