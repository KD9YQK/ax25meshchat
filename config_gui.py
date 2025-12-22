#!/usr/bin/env python3
"""
gui_config.py

A small wxPython-based config editor for config.yaml.

Design goals:
- Explain every option with a tooltip.
- Edit peers in a simple list (add/edit/remove).
- Save back to YAML using safe_dump.
- Keep this GUI-only (no attempt to hot-reload the backend).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import wx

try:
    import yaml  # type: ignore
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "PyYAML is required for config_gui.py (pip install pyyaml)."
    ) from exc

# -----------------------------
# Tooltips (single source of truth)
# -----------------------------

TOOLTIPS: Dict[str, str] = {
    "mesh.callsign": "Your station callsign/SSID used as the node identifier on the mesh (e.g., KD9YQK-1).",
    "mesh.mesh_dest_callsign": "The destination callsign used for mesh frames (a shared 'group' callsign, e.g., QMESH-0).",

    "ardop.host": "IP/hostname of the ARDOP TNC/host interface (usually 127.0.0.1 if ardop is local).",
    "ardop.port": "TCP port for the ARDOP Host/TNC interface (default is often 8515).",
    "ardop.reconnect_base_delay": "Seconds to wait before retrying after a disconnect (base delay; exponential backoff may apply).",
    "ardop.reconnect_max_delay": "Maximum seconds between reconnect attempts (caps the backoff).",
    "ardop.tx_queue_size": "Max number of outbound payloads queued for transmit (prevents unbounded memory growth).",

    "routing.ogm_interval_seconds": "Seconds between OGM (Originator) beacons. Longer is quieter on RF; shorter converges faster.",
    "routing.ogm_ttl": "How many hops an OGM is forwarded (mesh diameter limit).",
    "routing.route_expiry_seconds": "How long a route stays valid without updates before expiring.",
    "routing.neighbor_expiry_seconds": "How long a neighbor is kept 'alive' without hearing from it.",
    "routing.data_seen_expiry_seconds": "How long deduplication entries are kept (prevents loops/duplicates).",

    "security.enable_encryption": "Encryption MUST remain false for ham bands. Enable only on legal non-amateur links.",
    "security.key_hex": "Hex-encoded key material used by your crypto layer (leave null/blank when encryption is disabled).",

    "chat.db_path": "Path to the SQLite chat log database (relative paths resolve from the working directory).",

    "chat.sync": "Settings for message history synchronization between nodes.",
    "chat.sync.enabled": "Master toggle for sync logic. If false, the client will not request or respond with history sync data.",
    "chat.sync.last_n_messages": "How many recent messages to include per channel/DM when syncing (window size).",
    "chat.sync.max_send_per_response": "Maximum messages to include in any single sync response (caps burst size).",
    "chat.sync.auto_sync_on_new_peer": "If true, automatically initiate sync when a new peer is discovered.",
    "chat.sync.min_sync_interval_seconds": "Minimum seconds between sync attempts for the same peer/channel (cooldown).",

    "chat.sync.targeted_sync": "Targeted sync (range-based) tuning. Controls how confirmed gaps are turned into range sync requests.",
    "chat.sync.targeted_sync.enabled": "Enable targeted (range-based) sync. If false, the node will not issue range sync requests.",
    "chat.sync.targeted_sync.merge_distance": "Coalescing distance (in seqno units). 0 merges only overlapping/adjacent ranges; larger values merge 'nearby' gaps to reduce request count.",
    "chat.sync.targeted_sync.max_range_len": "Maximum length (seqnos) per individual range request. Larger values reduce request overhead but can increase response size.",
    "chat.sync.targeted_sync.max_requests_per_trigger": "Maximum number of range requests sent per confirmed-gap trigger (caps burstiness on RF).",
    "chat.peers": "Known peers you want to address by nickname. Keys are local aliases (e.g., 'bob').",
    "chat.peer_key": "Local alias for the peer (used in config file; not transmitted).",
    "chat.peer.node_id_hex": "Peer node ID as hex (what your mesh uses as node ID).",
    "chat.peer.nick": "Display nickname for that peer (used in UI/history).",

    "gui": "GUI appearance settings (colors and font sizes).",
    "gui.colors": "Hex colors for GUI elements (e.g., '#1e1e1e').",
    "gui.colors.window_bg": "Background color for the main window and panels.",
    "gui.colors.chat_bg": "Chat transcript background color.",
    "gui.colors.chat_fg": "Chat transcript foreground/text color.",
    "gui.colors.input_bg": "Input textbox background color.",
    "gui.colors.input_fg": "Input textbox foreground/text color.",
    "gui.colors.list_bg": "Left list (nodes/channels) background color.",
    "gui.colors.list_fg": "Left list (nodes/channels) foreground/text color.",
    "gui.colors.status_bg": "Status bar background color.",
    "gui.colors.status_fg": "Status bar foreground/text color.",
    "gui.colors.me": "Color used to highlight your own callsign/nick in chat.",
    "gui.colors.known": "Color used to highlight known peers in chat.",
    "gui.colors.unknown": "Color used to highlight unknown senders in chat.",

    "gui.font_sizes": "Font sizes (points) used by different GUI elements.",
    "gui.font_sizes.chat": "Font size for chat transcript.",
    "gui.font_sizes.input": "Font size for the message input textbox.",
    "gui.font_sizes.list": "Font size for the nodes/channels list.",
    "gui.font_sizes.status": "Font size for the status bar.",
}


def _deep_get(d: Dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _deep_set(d: Dict[str, Any], path: str, value: Any) -> None:
    cur: Any = d
    parts = path.split(".")
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def load_config_yaml(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml must contain a YAML mapping at top level.")
    return data


def save_config_yaml(path: str, data: Dict[str, Any]) -> None:
    p = Path(path)
    text = yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    p.write_text(text, encoding="utf-8")


@dataclass
class PeerRow:
    key: str
    node_id_hex: str
    nick: str


class PeerEditDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, title: str, initial: Optional[PeerRow] = None) -> None:
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)

        grid = wx.FlexGridSizer(rows=3, cols=2, vgap=8, hgap=8)
        grid.AddGrowableCol(1, 1)

        self.key_ctrl = wx.TextCtrl(self)
        self.key_ctrl.SetToolTip(TOOLTIPS["chat.peer_key"])

        self.node_id_ctrl = wx.TextCtrl(self)
        self.node_id_ctrl.SetToolTip(TOOLTIPS["chat.peer.node_id_hex"])

        self.nick_ctrl = wx.TextCtrl(self)
        self.nick_ctrl.SetToolTip(TOOLTIPS["chat.peer.nick"])

        grid.Add(wx.StaticText(self, label="Peer key"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.key_ctrl, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label="Node ID (hex)"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.node_id_ctrl, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label="Nick"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.nick_ctrl, 1, wx.EXPAND)

        sizer.Add(grid, 0, wx.EXPAND | wx.ALL, 12)

        btns = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        if btns:
            sizer.Add(btns, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        self.SetSizerAndFit(sizer)
        self.SetMinSize(wx.Size(420, -1))

        if initial:
            self.key_ctrl.SetValue(initial.key)
            self.node_id_ctrl.SetValue(initial.node_id_hex)
            self.nick_ctrl.SetValue(initial.nick)

        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

    def _on_ok(self, event: wx.CommandEvent) -> None:
        key = self.key_ctrl.GetValue().strip()
        node_id = self.node_id_ctrl.GetValue().strip()
        nick = self.nick_ctrl.GetValue().strip()

        if not key:
            wx.MessageBox("Peer key cannot be empty.", "Validation", wx.ICON_WARNING)
            return

        if node_id:
            import re
            if not re.fullmatch(r"[0-9a-fA-F]+", node_id) or (len(node_id) % 2 != 0):
                wx.MessageBox("Node ID hex must be hex characters and have an even length.", "Validation",
                              wx.ICON_WARNING)
                return

        if not nick:
            wx.MessageBox("Nick cannot be empty.", "Validation", wx.ICON_WARNING)
            return

        event.Skip()

    def get_peer(self) -> PeerRow:
        return PeerRow(
            key=self.key_ctrl.GetValue().strip(),
            node_id_hex=self.node_id_ctrl.GetValue().strip(),
            nick=self.nick_ctrl.GetValue().strip(),
        )


class ConfigEditorDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, config_path: str) -> None:
        super().__init__(parent, title="Edit Config (config.yaml)", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.config_path = config_path
        self.data: Dict[str, Any] = load_config_yaml(config_path)

        outer = wx.BoxSizer(wx.VERTICAL)
        self.nb = wx.Notebook(self)
        outer.Add(self.nb, 1, wx.EXPAND | wx.ALL, 10)

        self._build_mesh_tab()
        self._build_ardop_tab()
        self._build_routing_tab()
        self._build_security_tab()
        self._build_chat_tab()
        self._build_gui_tab()

        hint = wx.StaticText(
            self,
            label="Note: Saving updates config.yaml on disk. Restart the app to apply changes to a running backend.",
        )
        outer.Add(hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        btns = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        if btns:
            outer.Add(btns, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.SetSizerAndFit(outer)
        self.SetMinSize(wx.Size(700, 520))

        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

    @staticmethod
    def _make_labeled(parent: wx.Window, label: str, ctrl: wx.Window, tip: Optional[str] = None) -> wx.Sizer:
        s = wx.BoxSizer(wx.HORIZONTAL)
        st = wx.StaticText(parent, label=label, size=wx.Size(220, -1))
        s.Add(st, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        s.Add(ctrl, 1, wx.EXPAND)
        if tip:
            ctrl.SetToolTip(tip)
            st.SetToolTip(tip)
        return s

    @staticmethod
    def _float_ctrl(parent: wx.Window, value: Any) -> wx.TextCtrl:
        return wx.TextCtrl(parent, value="" if value is None else str(value))

    def _build_mesh_tab(self) -> None:
        panel = wx.Panel(self.nb)
        vs = wx.BoxSizer(wx.VERTICAL)

        self.callsign = wx.TextCtrl(panel, value=str(_deep_get(self.data, "mesh.callsign", "")))
        vs.Add(self._make_labeled(panel, "Callsign", self.callsign, TOOLTIPS["mesh.callsign"]), 0, wx.EXPAND | wx.ALL,
               6)

        self.mesh_dest = wx.TextCtrl(panel, value=str(_deep_get(self.data, "mesh.mesh_dest_callsign", "")))
        vs.Add(self._make_labeled(panel, "Mesh dest callsign", self.mesh_dest, TOOLTIPS["mesh.mesh_dest_callsign"]), 0,
               wx.EXPAND | wx.ALL, 6)

        panel.SetSizer(vs)
        self.nb.AddPage(panel, "Mesh")

    def _build_ardop_tab(self) -> None:
        panel = wx.Panel(self.nb)
        vs = wx.BoxSizer(wx.VERTICAL)

        self.ardop_host = wx.TextCtrl(panel, value=str(_deep_get(self.data, "ardop.host", "")))
        vs.Add(self._make_labeled(panel, "Host", self.ardop_host, TOOLTIPS["ardop.host"]), 0, wx.EXPAND | wx.ALL, 6)

        self.ardop_port = wx.SpinCtrl(panel, min=1, max=65535, initial=int(_deep_get(self.data, "ardop.port", 8515)))
        vs.Add(self._make_labeled(panel, "Port", self.ardop_port, TOOLTIPS["ardop.port"]), 0, wx.EXPAND | wx.ALL, 6)

        self.reconnect_base = self._float_ctrl(panel, _deep_get(self.data, "ardop.reconnect_base_delay", 5.0))
        vs.Add(self._make_labeled(panel, "Reconnect base delay (s)", self.reconnect_base,
                                  TOOLTIPS["ardop.reconnect_base_delay"]), 0, wx.EXPAND | wx.ALL, 6)

        self.reconnect_max = self._float_ctrl(panel, _deep_get(self.data, "ardop.reconnect_max_delay", 60.0))
        vs.Add(self._make_labeled(panel, "Reconnect max delay (s)", self.reconnect_max,
                                  TOOLTIPS["ardop.reconnect_max_delay"]), 0, wx.EXPAND | wx.ALL, 6)

        self.tx_queue = wx.SpinCtrl(panel, min=1, max=1_000_000,
                                    initial=int(_deep_get(self.data, "ardop.tx_queue_size", 1000)))
        vs.Add(self._make_labeled(panel, "TX queue size", self.tx_queue, TOOLTIPS["ardop.tx_queue_size"]), 0,
               wx.EXPAND | wx.ALL, 6)

        panel.SetSizer(vs)
        self.nb.AddPage(panel, "ARDOP")

    def _build_routing_tab(self) -> None:
        panel = wx.Panel(self.nb)
        vs = wx.BoxSizer(wx.VERTICAL)

        self.ogm_interval = self._float_ctrl(panel, _deep_get(self.data, "routing.ogm_interval_seconds", 600.0))
        vs.Add(
            self._make_labeled(panel, "OGM interval (s)", self.ogm_interval, TOOLTIPS["routing.ogm_interval_seconds"]),
            0, wx.EXPAND | wx.ALL, 6)

        self.ogm_ttl = wx.SpinCtrl(panel, min=1, max=255, initial=int(_deep_get(self.data, "routing.ogm_ttl", 5)))
        vs.Add(self._make_labeled(panel, "OGM TTL (hops)", self.ogm_ttl, TOOLTIPS["routing.ogm_ttl"]), 0,
               wx.EXPAND | wx.ALL, 6)

        self.route_expiry = self._float_ctrl(panel, _deep_get(self.data, "routing.route_expiry_seconds", 1200.0))
        vs.Add(
            self._make_labeled(panel, "Route expiry (s)", self.route_expiry, TOOLTIPS["routing.route_expiry_seconds"]),
            0, wx.EXPAND | wx.ALL, 6)

        self.neighbor_expiry = self._float_ctrl(panel, _deep_get(self.data, "routing.neighbor_expiry_seconds", 610.0))
        vs.Add(self._make_labeled(panel, "Neighbor expiry (s)", self.neighbor_expiry,
                                  TOOLTIPS["routing.neighbor_expiry_seconds"]), 0, wx.EXPAND | wx.ALL, 6)

        self.data_seen_expiry = self._float_ctrl(panel, _deep_get(self.data, "routing.data_seen_expiry_seconds", 610.0))
        vs.Add(self._make_labeled(panel, "Dedup cache expiry (s)", self.data_seen_expiry,
                                  TOOLTIPS["routing.data_seen_expiry_seconds"]), 0, wx.EXPAND | wx.ALL, 6)

        panel.SetSizer(vs)
        self.nb.AddPage(panel, "Routing")

    def _build_security_tab(self) -> None:
        panel = wx.Panel(self.nb)
        vs = wx.BoxSizer(wx.VERTICAL)

        self.enable_encryption = wx.CheckBox(panel, label="Enable encryption")
        self.enable_encryption.SetValue(bool(_deep_get(self.data, "security.enable_encryption", False)))
        self.enable_encryption.SetToolTip(TOOLTIPS["security.enable_encryption"])
        vs.Add(self.enable_encryption, 0, wx.ALL, 8)

        self.key_hex = wx.TextCtrl(panel, value="" if _deep_get(self.data, "security.key_hex", None) is None else str(
            _deep_get(self.data, "security.key_hex")))
        vs.Add(self._make_labeled(panel, "Key (hex)", self.key_hex, TOOLTIPS["security.key_hex"]), 0,
               wx.EXPAND | wx.ALL, 6)

        warn = wx.StaticText(
            panel,
            label="Ham radio note: encryption is generally prohibited on amateur bands (with narrow exceptions).",
        )
        vs.Add(warn, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)

        panel.SetSizer(vs)
        self.nb.AddPage(panel, "Security")

    def _build_chat_tab(self) -> None:
        panel = wx.Panel(self.nb)
        vs = wx.BoxSizer(wx.VERTICAL)

        self.db_path = wx.TextCtrl(panel, value=str(_deep_get(self.data, "chat.db_path", "chat_logs.sqlite")))
        vs.Add(self._make_labeled(panel, "DB path", self.db_path, TOOLTIPS["chat.db_path"]), 0, wx.EXPAND | wx.ALL, 6)

        # -----------------------
        # Sync options
        # -----------------------
        sync_label = wx.StaticText(panel, label="Sync")
        sync_label.SetToolTip(TOOLTIPS["chat.sync"])
        vs.Add(sync_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)

        sync_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "")
        sync_box.GetStaticBox().SetToolTip(TOOLTIPS["chat.sync"])

        self.sync_enabled = wx.CheckBox(sync_box.GetStaticBox(), label="Enable sync")
        self.sync_enabled.SetValue(bool(_deep_get(self.data, "chat.sync.enabled", True)))
        self.sync_enabled.SetToolTip(TOOLTIPS["chat.sync.enabled"])
        sync_box.Add(self.sync_enabled, 0, wx.ALL, 6)

        self.sync_last_n = wx.SpinCtrl(sync_box.GetStaticBox(), min=0, max=100_000,
                                       initial=int(_deep_get(self.data, "chat.sync.last_n_messages", 200)))
        sync_box.Add(self._make_labeled(sync_box.GetStaticBox(), "Last N messages", self.sync_last_n,
                                        TOOLTIPS["chat.sync.last_n_messages"]), 0, wx.EXPAND | wx.ALL, 6)

        self.sync_max_send = wx.SpinCtrl(sync_box.GetStaticBox(), min=0, max=100_000,
                                         initial=int(_deep_get(self.data, "chat.sync.max_send_per_response", 200)))
        sync_box.Add(self._make_labeled(sync_box.GetStaticBox(), "Max send per response", self.sync_max_send,
                                        TOOLTIPS["chat.sync.max_send_per_response"]), 0, wx.EXPAND | wx.ALL, 6)

        self.sync_auto_on_new_peer = wx.CheckBox(sync_box.GetStaticBox(), label="Auto-sync on new peer")
        self.sync_auto_on_new_peer.SetValue(bool(_deep_get(self.data, "chat.sync.auto_sync_on_new_peer", True)))
        self.sync_auto_on_new_peer.SetToolTip(TOOLTIPS["chat.sync.auto_sync_on_new_peer"])
        sync_box.Add(self.sync_auto_on_new_peer, 0, wx.ALL, 6)

        self.sync_min_interval = wx.SpinCtrl(sync_box.GetStaticBox(), min=0, max=86_400, initial=int(
            _deep_get(self.data, "chat.sync.min_sync_interval_seconds", 30)))
        sync_box.Add(self._make_labeled(sync_box.GetStaticBox(), "Min sync interval (s)", self.sync_min_interval,
                                        TOOLTIPS["chat.sync.min_sync_interval_seconds"]), 0, wx.EXPAND | wx.ALL, 6)

        vs.Add(sync_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # -----------------------
        # Targeted sync (range-based) options
        # -----------------------
        targeted_label = wx.StaticText(panel, label="Targeted sync (range)")
        targeted_label.SetToolTip(TOOLTIPS["chat.sync.targeted_sync"])
        vs.Add(targeted_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)

        targeted_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "")
        targeted_box.GetStaticBox().SetToolTip(TOOLTIPS["chat.sync.targeted_sync"])

        self.targeted_sync_enabled = wx.CheckBox(targeted_box.GetStaticBox(), label="Enable targeted sync")
        self.targeted_sync_enabled.SetValue(bool(_deep_get(self.data, "chat.sync.targeted_sync.enabled", True)))
        self.targeted_sync_enabled.SetToolTip(TOOLTIPS["chat.sync.targeted_sync.enabled"])
        targeted_box.Add(self.targeted_sync_enabled, 0, wx.ALL, 6)

        self.targeted_merge_distance = wx.SpinCtrl(
            targeted_box.GetStaticBox(),
            min=0,
            max=1_000_000,
            initial=int(_deep_get(self.data, "chat.sync.targeted_sync.merge_distance", 0)),
        )
        targeted_box.Add(
            self._make_labeled(
                targeted_box.GetStaticBox(),
                "Merge distance (seqnos)",
                self.targeted_merge_distance,
                TOOLTIPS["chat.sync.targeted_sync.merge_distance"],
            ),
            0,
            wx.EXPAND | wx.ALL,
            6,
        )

        self.targeted_max_range_len = wx.SpinCtrl(
            targeted_box.GetStaticBox(),
            min=1,
            max=1_000_000,
            initial=int(_deep_get(self.data, "chat.sync.targeted_sync.max_range_len", 50)),
        )
        targeted_box.Add(
            self._make_labeled(
                targeted_box.GetStaticBox(),
                "Max range length",
                self.targeted_max_range_len,
                TOOLTIPS["chat.sync.targeted_sync.max_range_len"],
            ),
            0,
            wx.EXPAND | wx.ALL,
            6,
        )

        self.targeted_max_requests = wx.SpinCtrl(
            targeted_box.GetStaticBox(),
            min=1,
            max=1_000_000,
            initial=int(_deep_get(self.data, "chat.sync.targeted_sync.max_requests_per_trigger", 3)),
        )
        targeted_box.Add(
            self._make_labeled(
                targeted_box.GetStaticBox(),
                "Max requests per trigger",
                self.targeted_max_requests,
                TOOLTIPS["chat.sync.targeted_sync.max_requests_per_trigger"],
            ),
            0,
            wx.EXPAND | wx.ALL,
            6,
        )

        vs.Add(targeted_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        peers_label = wx.StaticText(panel, label="Peers")
        peers_label.SetToolTip(TOOLTIPS["chat.peers"])
        vs.Add(peers_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)

        hs = wx.BoxSizer(wx.HORIZONTAL)

        self.peers_list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.peers_list.InsertColumn(0, "Key", width=140)
        self.peers_list.InsertColumn(1, "Node ID (hex)", width=220)
        self.peers_list.InsertColumn(2, "Nick", width=180)
        self.peers_list.SetToolTip(TOOLTIPS["chat.peers"])
        hs.Add(self.peers_list, 1, wx.EXPAND | wx.ALL, 6)

        btns = wx.BoxSizer(wx.VERTICAL)
        self.btn_add_peer = wx.Button(panel, label="Add…")
        self.btn_edit_peer = wx.Button(panel, label="Edit…")
        self.btn_remove_peer = wx.Button(panel, label="Remove")
        btns.Add(self.btn_add_peer, 0, wx.EXPAND | wx.BOTTOM, 6)
        btns.Add(self.btn_edit_peer, 0, wx.EXPAND | wx.BOTTOM, 6)
        btns.Add(self.btn_remove_peer, 0, wx.EXPAND)
        hs.Add(btns, 0, wx.TOP | wx.RIGHT | wx.BOTTOM, 6)

        vs.Add(hs, 1, wx.EXPAND)

        self.btn_add_peer.Bind(wx.EVT_BUTTON, self._on_add_peer)
        self.btn_edit_peer.Bind(wx.EVT_BUTTON, self._on_edit_peer)
        self.btn_remove_peer.Bind(wx.EVT_BUTTON, self._on_remove_peer)

        self._load_peers_into_list()

        panel.SetSizer(vs)
        self.nb.AddPage(panel, "Chat")

    @staticmethod
    def _color_ctrl(parent: wx.Window, value: Any) -> wx.ColourPickerCtrl:
        # Accept #RRGGBB strings; fall back to default control color.
        raw = str(value) if value is not None else ""
        col = wx.NullColour
        if raw.startswith("#") and len(raw) == 7:
            try:
                col = wx.Colour(raw)
            except (ValueError, TypeError):
                col = wx.NullColour
        return wx.ColourPickerCtrl(parent, colour=col)

    @staticmethod
    def _color_to_hex(col: wx.Colour) -> str:
        return f"#{col.Red():02x}{col.Green():02x}{col.Blue():02x}"

    def _build_gui_tab(self) -> None:
        panel = wx.Panel(self.nb)
        vs = wx.BoxSizer(wx.VERTICAL)

        colors_label = wx.StaticText(panel, label="Colors")
        colors_label.SetToolTip(TOOLTIPS["gui.colors"])
        vs.Add(colors_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)

        colors_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "")
        colors_box.GetStaticBox().SetToolTip(TOOLTIPS["gui.colors"])

        self.gui_window_bg = self._color_ctrl(panel, _deep_get(self.data, "gui.colors.window_bg", None))
        colors_box.Add(self._make_labeled(panel, "Window background", self.gui_window_bg, TOOLTIPS["gui.colors.window_bg"]), 0, wx.EXPAND | wx.ALL, 6)

        self.gui_chat_bg = self._color_ctrl(panel, _deep_get(self.data, "gui.colors.chat_bg", None))
        colors_box.Add(self._make_labeled(panel, "Chat background", self.gui_chat_bg, TOOLTIPS["gui.colors.chat_bg"]), 0, wx.EXPAND | wx.ALL, 6)

        self.gui_chat_fg = self._color_ctrl(panel, _deep_get(self.data, "gui.colors.chat_fg", None))
        colors_box.Add(self._make_labeled(panel, "Chat text", self.gui_chat_fg, TOOLTIPS["gui.colors.chat_fg"]), 0, wx.EXPAND | wx.ALL, 6)

        self.gui_input_bg = self._color_ctrl(panel, _deep_get(self.data, "gui.colors.input_bg", None))
        colors_box.Add(self._make_labeled(panel, "Input background", self.gui_input_bg, TOOLTIPS["gui.colors.input_bg"]), 0, wx.EXPAND | wx.ALL, 6)

        self.gui_input_fg = self._color_ctrl(panel, _deep_get(self.data, "gui.colors.input_fg", None))
        colors_box.Add(self._make_labeled(panel, "Input text", self.gui_input_fg, TOOLTIPS["gui.colors.input_fg"]), 0, wx.EXPAND | wx.ALL, 6)

        self.gui_list_bg = self._color_ctrl(panel, _deep_get(self.data, "gui.colors.list_bg", None))
        colors_box.Add(self._make_labeled(panel, "List background", self.gui_list_bg, TOOLTIPS["gui.colors.list_bg"]), 0, wx.EXPAND | wx.ALL, 6)

        self.gui_list_fg = self._color_ctrl(panel, _deep_get(self.data, "gui.colors.list_fg", None))
        colors_box.Add(self._make_labeled(panel, "List text", self.gui_list_fg, TOOLTIPS["gui.colors.list_fg"]), 0, wx.EXPAND | wx.ALL, 6)

        # Sender highlight colors
        self.gui_me = self._color_ctrl(panel, _deep_get(self.data, "gui.colors.me", None))
        colors_box.Add(self._make_labeled(panel, "Highlight: me", self.gui_me, TOOLTIPS["gui.colors.me"]), 0, wx.EXPAND | wx.ALL, 6)

        self.gui_known = self._color_ctrl(panel, _deep_get(self.data, "gui.colors.known", None))
        colors_box.Add(self._make_labeled(panel, "Highlight: known", self.gui_known, TOOLTIPS["gui.colors.known"]), 0, wx.EXPAND | wx.ALL, 6)

        self.gui_unknown = self._color_ctrl(panel, _deep_get(self.data, "gui.colors.unknown", None))
        colors_box.Add(self._make_labeled(panel, "Highlight: unknown", self.gui_unknown, TOOLTIPS["gui.colors.unknown"]), 0, wx.EXPAND | wx.ALL, 6)

        vs.Add(colors_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        fonts_label = wx.StaticText(panel, label="Font sizes")
        fonts_label.SetToolTip(TOOLTIPS["gui.font_sizes"])
        vs.Add(fonts_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)

        fonts_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "")
        fonts_box.GetStaticBox().SetToolTip(TOOLTIPS["gui.font_sizes"])

        self.gui_font_chat = wx.SpinCtrl(panel, min=6, max=48, initial=int(_deep_get(self.data, "gui.font_sizes.chat", 10)))
        fonts_box.Add(self._make_labeled(panel, "Chat", self.gui_font_chat, TOOLTIPS["gui.font_sizes.chat"]), 0, wx.EXPAND | wx.ALL, 6)

        self.gui_font_input = wx.SpinCtrl(panel, min=6, max=48, initial=int(_deep_get(self.data, "gui.font_sizes.input", 10)))
        fonts_box.Add(self._make_labeled(panel, "Input", self.gui_font_input, TOOLTIPS["gui.font_sizes.input"]), 0, wx.EXPAND | wx.ALL, 6)

        self.gui_font_list = wx.SpinCtrl(panel, min=6, max=48, initial=int(_deep_get(self.data, "gui.font_sizes.list", 10)))
        fonts_box.Add(self._make_labeled(panel, "List", self.gui_font_list, TOOLTIPS["gui.font_sizes.list"]), 0, wx.EXPAND | wx.ALL, 6)

        vs.Add(fonts_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        panel.SetSizer(vs)
        self.nb.AddPage(panel, "GUI")

    def _load_peers_into_list(self) -> None:
        self.peers_list.DeleteAllItems()
        peers = _deep_get(self.data, "chat.peers", {}) or {}
        if not isinstance(peers, dict):
            peers = {}

        for key in sorted(peers.keys()):
            node_id = str(peers.get(key, {}).get("node_id_hex", "") or "")
            nick = str(peers.get(key, {}).get("nick", "") or "")
            idx = self.peers_list.InsertItem(self.peers_list.GetItemCount(), key)
            self.peers_list.SetItem(idx, 1, node_id)
            self.peers_list.SetItem(idx, 2, nick)

    def _get_selected_peer_key(self) -> Optional[str]:
        idx = self.peers_list.GetFirstSelected()
        if idx == -1:
            return None
        return self.peers_list.GetItemText(idx)

    def _on_add_peer(self, _event: wx.CommandEvent) -> None:
        dlg = PeerEditDialog(self, "Add Peer")
        if dlg.ShowModal() == wx.ID_OK:
            row = dlg.get_peer()
            peers = _deep_get(self.data, "chat.peers", {}) or {}
            if not isinstance(peers, dict):
                peers = {}
            if row.key in peers:
                wx.MessageBox(f"Peer key '{row.key}' already exists.", "Validation", wx.ICON_WARNING)
                dlg.Destroy()
                return
            peers[row.key] = {"node_id_hex": row.node_id_hex, "nick": row.nick}
            _deep_set(self.data, "chat.peers", peers)
            self._load_peers_into_list()
        dlg.Destroy()

    def _on_edit_peer(self, _event: wx.CommandEvent) -> None:
        key = self._get_selected_peer_key()
        if not key:
            wx.MessageBox("Select a peer to edit.", "Peers", wx.ICON_INFORMATION)
            return
        peers = _deep_get(self.data, "chat.peers", {}) or {}
        entry = peers.get(key, {}) if isinstance(peers, dict) else {}
        initial = PeerRow(
            key=key,
            node_id_hex=str(entry.get("node_id_hex", "") or ""),
            nick=str(entry.get("nick", "") or ""),
        )
        dlg = PeerEditDialog(self, "Edit Peer", initial=initial)
        if dlg.ShowModal() == wx.ID_OK:
            row = dlg.get_peer()
            if not isinstance(peers, dict):
                peers = {}
            if row.key != key and row.key in peers:
                wx.MessageBox(f"Peer key '{row.key}' already exists.", "Validation", wx.ICON_WARNING)
                dlg.Destroy()
                return
            if row.key != key:
                peers.pop(key, None)
            peers[row.key] = {"node_id_hex": row.node_id_hex, "nick": row.nick}
            _deep_set(self.data, "chat.peers", peers)
            self._load_peers_into_list()
        dlg.Destroy()

    def _on_remove_peer(self, _event: wx.CommandEvent) -> None:
        key = self._get_selected_peer_key()
        if not key:
            wx.MessageBox("Select a peer to remove.", "Peers", wx.ICON_INFORMATION)
            return
        if wx.MessageBox(f"Remove peer '{key}'?", "Confirm", wx.ICON_QUESTION | wx.YES_NO) != wx.YES:
            return
        peers = _deep_get(self.data, "chat.peers", {}) or {}
        if isinstance(peers, dict):
            peers.pop(key, None)
            _deep_set(self.data, "chat.peers", peers)
        self._load_peers_into_list()

    @staticmethod
    def _parse_float(label: str, ctrl: wx.TextCtrl) -> Optional[float]:
        raw = ctrl.GetValue().strip()
        if raw == "":
            wx.MessageBox(f"{label} cannot be blank.", "Validation", wx.ICON_WARNING)
            return None
        try:
            return float(raw)
        except ValueError:
            wx.MessageBox(f"{label} must be a number.", "Validation", wx.ICON_WARNING)
            return None

    def _on_ok(self, event: wx.CommandEvent) -> None:
        callsign = self.callsign.GetValue().strip()
        mesh_dest = self.mesh_dest.GetValue().strip()
        if not callsign or not mesh_dest:
            wx.MessageBox("Mesh callsign and mesh destination callsign cannot be blank.", "Validation", wx.ICON_WARNING)
            return

        reconnect_base = self._parse_float("Reconnect base delay", self.reconnect_base)
        if reconnect_base is None:
            return
        reconnect_max = self._parse_float("Reconnect max delay", self.reconnect_max)
        if reconnect_max is None:
            return
        ogm_interval = self._parse_float("OGM interval", self.ogm_interval)
        if ogm_interval is None:
            return
        route_expiry = self._parse_float("Route expiry", self.route_expiry)
        if route_expiry is None:
            return
        neighbor_expiry = self._parse_float("Neighbor expiry", self.neighbor_expiry)
        if neighbor_expiry is None:
            return
        data_seen_expiry = self._parse_float("Dedup cache expiry", self.data_seen_expiry)
        if data_seen_expiry is None:
            return

        enable_enc = bool(self.enable_encryption.GetValue())
        key_hex = self.key_hex.GetValue().strip()
        if enable_enc and not key_hex:
            wx.MessageBox("Encryption is enabled but key_hex is blank.", "Validation", wx.ICON_WARNING)
            return

        _deep_set(self.data, "mesh.callsign", callsign)
        _deep_set(self.data, "mesh.mesh_dest_callsign", mesh_dest)

        _deep_set(self.data, "ardop.host", self.ardop_host.GetValue().strip())
        _deep_set(self.data, "ardop.port", int(self.ardop_port.GetValue()))
        _deep_set(self.data, "ardop.reconnect_base_delay", float(reconnect_base))
        _deep_set(self.data, "ardop.reconnect_max_delay", float(reconnect_max))
        _deep_set(self.data, "ardop.tx_queue_size", int(self.tx_queue.GetValue()))

        _deep_set(self.data, "routing.ogm_interval_seconds", float(ogm_interval))
        _deep_set(self.data, "routing.ogm_ttl", int(self.ogm_ttl.GetValue()))
        _deep_set(self.data, "routing.route_expiry_seconds", float(route_expiry))
        _deep_set(self.data, "routing.neighbor_expiry_seconds", float(neighbor_expiry))
        _deep_set(self.data, "routing.data_seen_expiry_seconds", float(data_seen_expiry))

        _deep_set(self.data, "security.enable_encryption", enable_enc)
        _deep_set(self.data, "security.key_hex", (key_hex if key_hex else None))
        # chat.sync
        _deep_set(self.data, "chat.sync.enabled", bool(self.sync_enabled.GetValue()))
        _deep_set(self.data, "chat.sync.last_n_messages", int(self.sync_last_n.GetValue()))
        _deep_set(self.data, "chat.sync.max_send_per_response", int(self.sync_max_send.GetValue()))
        _deep_set(self.data, "chat.sync.auto_sync_on_new_peer", bool(self.sync_auto_on_new_peer.GetValue()))
        _deep_set(self.data, "chat.sync.min_sync_interval_seconds", int(self.sync_min_interval.GetValue()))

        # chat.sync.targeted_sync
        _deep_set(self.data, "chat.sync.targeted_sync.enabled", bool(self.targeted_sync_enabled.GetValue()))
        _deep_set(self.data, "chat.sync.targeted_sync.merge_distance", int(self.targeted_merge_distance.GetValue()))
        _deep_set(self.data, "chat.sync.targeted_sync.max_range_len", int(self.targeted_max_range_len.GetValue()))
        _deep_set(
            self.data,
            "chat.sync.targeted_sync.max_requests_per_trigger",
            int(self.targeted_max_requests.GetValue()),
        )

        _deep_set(self.data, "chat.db_path", self.db_path.GetValue().strip())

        # gui theme
        _deep_set(self.data, "gui.colors.window_bg", self._color_to_hex(self.gui_window_bg.GetColour()))
        _deep_set(self.data, "gui.colors.chat_bg", self._color_to_hex(self.gui_chat_bg.GetColour()))
        _deep_set(self.data, "gui.colors.chat_fg", self._color_to_hex(self.gui_chat_fg.GetColour()))
        _deep_set(self.data, "gui.colors.input_bg", self._color_to_hex(self.gui_input_bg.GetColour()))
        _deep_set(self.data, "gui.colors.input_fg", self._color_to_hex(self.gui_input_fg.GetColour()))
        _deep_set(self.data, "gui.colors.list_bg", self._color_to_hex(self.gui_list_bg.GetColour()))
        _deep_set(self.data, "gui.colors.list_fg", self._color_to_hex(self.gui_list_fg.GetColour()))
        _deep_set(self.data, "gui.colors.me", self._color_to_hex(self.gui_me.GetColour()))
        _deep_set(self.data, "gui.colors.known", self._color_to_hex(self.gui_known.GetColour()))
        _deep_set(self.data, "gui.colors.unknown", self._color_to_hex(self.gui_unknown.GetColour()))

        _deep_set(self.data, "gui.font_sizes.chat", int(self.gui_font_chat.GetValue()))
        _deep_set(self.data, "gui.font_sizes.input", int(self.gui_font_input.GetValue()))
        _deep_set(self.data, "gui.font_sizes.list", int(self.gui_font_list.GetValue()))

        try:
            save_config_yaml(self.config_path, self.data)
        except (OSError, ValueError, yaml.YAMLError) as e:
            wx.MessageBox(f"Failed to save config:\n{e}", "Error", wx.ICON_ERROR)
            return

        event.Skip()


def open_config_editor(parent: wx.Window, config_path: str) -> bool:
    """Open the config editor. Returns True if user saved (OK), False otherwise."""
    dlg = ConfigEditorDialog(parent, config_path=config_path)
    try:
        return dlg.ShowModal() == wx.ID_OK
    finally:
        dlg.Destroy()
