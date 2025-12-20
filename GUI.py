#!/usr/bin/env python3
"""
Mesh Chat GUI (wxPython version, no AUI manager)

Layout:
- Top: SplitterWindow
    - Left: Nodes / Channels list
    - Right: Tabbed chat (AuiNotebook with close buttons)
- Bottom: Input row (separate textbox + Send button) spanning full width
- Status bar at the bottom

Backend contract (same as Tkinter version):
- backend: BackendInterface
    - get_ui_queue() -> queue.Queue[UIEvent]
    - send_message(channel: str, text: str) -> None
    - shutdown() -> None
"""

from __future__ import annotations

import queue
import time
from typing import Optional

import wx
import wx.aui as aui
from config_gui import open_config_editor

from mesh_backend import (
    BackendInterface,
    ChatEvent,
    StatusEvent,
    UIEvent,
    MeshChatBackend,
    NodeListEvent,
    ChannelListEvent,
    HistoryEvent,
)


# =====================================================================
# Main Application Frame
# =====================================================================

class ChatFrame(wx.Frame):
    """
    Main wxPython frame:

    - splitter: left node list, right notebook
    - notebook: tabs per channel/DM
    - bottom input row
    """

    POLL_INTERVAL_MS = 100

    def __init__(self, backend: BackendInterface, cfg_path: str = "config.yaml") -> None:
        super().__init__(None, title="ARDOP Mesh Chat", size=wx.Size(1000, 700))
        self.backend = backend
        self.config_path = cfg_path
        self.ui_queue: queue.Queue[UIEvent] = backend.get_ui_queue()
        self._status_tab_name = "Status"
        self._known_nodes: list[str] = []
        self._known_channels: list[str] = []
        self._history_loaded: set[str] = set()

        self._build_ui()

        # Preload history for #general (tab is created on demand; this won't steal focus)
        try:
            self.backend.request_history("#general", limit=200)
            self._history_loaded.add("#general")
        except AttributeError:
            pass

        # Timer for backend queue polling
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_timer, self._timer)
        self._timer.Start(self.POLL_INTERVAL_MS)

        self.Bind(wx.EVT_CLOSE, self.on_close)

    # -----------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------

    def _build_ui(self) -> None:
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # =======================
        # Menu bar (dropdown)
        # =======================
        menubar = wx.MenuBar()
        settings_menu = wx.Menu()
        mi_edit = settings_menu.Append(wx.ID_ANY, "Edit Config…\tCtrl+,", "Open config editor")
        self.Bind(wx.EVT_MENU, self.on_edit_config, mi_edit)
        menubar.Append(settings_menu, "Settings")
        self.SetMenuBar(menubar)

        # =======================
        # Top: Splitter
        # =======================
        splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)

        # Left panel: nodes / channels
        left_panel = wx.Panel(splitter)
        left_sizer = wx.BoxSizer(wx.VERTICAL)

        label = wx.StaticText(left_panel, label="Nodes / Channels")
        self.node_list = wx.ListCtrl(
            left_panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL,
        )
        self.node_list.InsertColumn(0, "Name", width=200)
        # Static channel(s). Dynamic nodes are populated from mesh state.
        self.node_list.InsertItem(0, "#general")

        self.node_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_node_activated)

        left_sizer.Add(label, 0, wx.EXPAND | wx.ALL, 4)
        left_sizer.Add(self.node_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)
        left_panel.SetSizer(left_sizer)

        # Right panel: notebook
        right_panel = wx.Panel(splitter)
        right_sizer = wx.BoxSizer(wx.VERTICAL)

        self.notebook = aui.AuiNotebook(
            right_panel,
            style=(
                    aui.AUI_NB_DEFAULT_STYLE
                    | aui.AUI_NB_TAB_MOVE
                    | aui.AUI_NB_CLOSE_ON_ACTIVE_TAB
            ),
        )
        self.notebook.Bind(aui.EVT_AUINOTEBOOK_PAGE_CLOSE, self.on_tab_close)

        right_sizer.Add(self.notebook, 1, wx.EXPAND)
        right_panel.SetSizer(right_sizer)

        splitter.SplitVertically(left_panel, right_panel, sashPosition=220)
        splitter.SetMinimumPaneSize(150)

        main_sizer.Add(splitter, 1, wx.EXPAND)

        # =======================
        # Bottom: input row
        # =======================
        bottom_panel = wx.Panel(self)
        bottom_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.input_box = wx.TextCtrl(bottom_panel, style=wx.TE_PROCESS_ENTER)
        self.input_box.Bind(wx.EVT_TEXT_ENTER, self.on_send)

        self.send_button = wx.Button(bottom_panel, label="Send")
        self.send_button.Bind(wx.EVT_BUTTON, self.on_send)

        bottom_sizer.Add(self.input_box, 1, wx.EXPAND | wx.ALL, 5)
        bottom_sizer.Add(self.send_button, 0, wx.ALL, 5)
        bottom_panel.SetSizer(bottom_sizer)

        main_sizer.Add(bottom_panel, 0, wx.EXPAND)

        # =======================
        # Status bar
        # =======================
        self.CreateStatusBar()
        self.GetStatusBar().SetFieldsCount(3)
        self.SetStatusText("Ready", 0)
        self.SetStatusText("Nodes: 0", 1)
        self.SetStatusText("", 2)

        self.SetSizer(main_sizer)

        # Create initial Status tab (select it at startup)
        self._ensure_tab(self._status_tab_name, select=True)

    # -----------------------------------------------------------------
    # Tab helpers
    # -----------------------------------------------------------------

    def _ensure_tab(self, name: str, select: bool) -> None:
        """
        Ensure a tab with the given name exists.

        - If it exists:
            - Optionally select it.
        - If it does not exist:
            - Create it, and select depending on `select`.
        """
        for idx in range(self.notebook.GetPageCount()):
            if self.notebook.GetPageText(idx) == name:
                if select:
                    self.notebook.SetSelection(idx)
                return

        # Create new tab
        panel = wx.Panel(self.notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)

        text_ctrl = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
        )
        sizer.Add(text_ctrl, 1, wx.EXPAND)

        panel.SetSizer(sizer)
        self.notebook.AddPage(panel, name, select=select)

    def _create_tab(self, name: str) -> None:
        """
        User-driven tab creation: ensure tab exists and select it.
        """
        self._ensure_tab(name, select=True)

    def _get_text_ctrl_for_tab(self, name: str) -> Optional[wx.TextCtrl]:
        for idx in range(self.notebook.GetPageCount()):
            if self.notebook.GetPageText(idx) == name:
                page = self.notebook.GetPage(idx)
                sizer = page.GetSizer()
                if not sizer or sizer.GetItemCount() == 0:
                    return None
                item = sizer.GetItem(0)
                ctrl = item.GetWindow()
                if isinstance(ctrl, wx.TextCtrl):
                    return ctrl
        return None

    def _append_to_tab(self, name: str, text: str) -> None:
        """
        Append a line of text to a tab, creating it if necessary,
        but never stealing focus from the user.
        """
        # Ensure the tab exists but DO NOT select it
        self._ensure_tab(name, select=False)
        ctrl = self._get_text_ctrl_for_tab(name)
        if ctrl is None:
            return
        ctrl.SetInsertionPointEnd()
        ctrl.WriteText(text)

    def _get_current_tab_name(self) -> str:
        idx = self.notebook.GetSelection()
        if idx == wx.NOT_FOUND:
            return self._status_tab_name
        return self.notebook.GetPageText(idx)

    # -----------------------------------------------------------------
    # Event handlers
    # -----------------------------------------------------------------

    def on_node_activated(self, event: wx.ListEvent) -> None:
        name = self.node_list.GetItemText(event.GetIndex())
        self._create_tab(name)
        self.SetStatusText(f"Active: {name}", 2)
        if name != self._status_tab_name and name not in self._history_loaded:
            try:
                self.backend.request_history(name, limit=200)
                self._history_loaded.add(name)
            except AttributeError:
                pass

    def on_send(self, _event: wx.CommandEvent) -> None:
        """
        User pressed Enter or clicked Send.

        - Append the message to the active chat tab.
        - Send it to the backend.
        """
        text = self.input_box.GetValue().strip()
        if not text:
            return

        tab_name = self._get_current_tab_name()
        if tab_name == self._status_tab_name:
            tab_name = "#general"

        # Local echo
        ts_str = time.strftime("%H:%M:%S", time.localtime())
        line = f"[{ts_str}] <me> {text}\n"
        self._append_to_tab(tab_name, line)

        self.input_box.SetValue("")
        self.backend.send_message(tab_name, text)

    def on_tab_close(self, event: aui.AuiNotebookEvent) -> None:
        """
        Prevent closing the Status tab.
        """
        idx = event.GetSelection()
        name = self.notebook.GetPageText(idx)
        if name == self._status_tab_name:
            event.Veto()

    # -----------------------------------------------------------------
    # Backend queue polling
    # -----------------------------------------------------------------

    def on_timer(self, _event: wx.TimerEvent) -> None:
        try:
            while True:
                ui_event = self.ui_queue.get_nowait()
                if isinstance(ui_event, ChatEvent):
                    self._render_chat_event(ui_event)
                elif isinstance(ui_event, StatusEvent):
                    self._render_status_event(ui_event)
                elif isinstance(ui_event, NodeListEvent):
                    self._render_node_list_event(ui_event)
                elif isinstance(ui_event, ChannelListEvent):
                    self._render_channel_list_event(ui_event)
                elif isinstance(ui_event, HistoryEvent):
                    self._render_history_event(ui_event)
        except queue.Empty:
            pass

    def on_edit_config(self, _event: wx.CommandEvent) -> None:
        """Open the YAML config editor dialog."""
        saved = open_config_editor(self, self.config_path)
        if saved:
            wx.MessageBox(
                "Saved config.yaml. Restart the app to apply changes to a running backend.",
                "Config Saved",
                wx.ICON_INFORMATION,
            )

    # -----------------------------------------------------------------
    # Rendering helpers
    # -----------------------------------------------------------------

    def _render_chat_event(self, ev: ChatEvent) -> None:
        ts_str = time.strftime("%H:%M:%S", time.localtime(ev.timestamp))
        line = f"[{ts_str}] <{ev.nick}> {ev.text}\n"
        self._append_to_tab(ev.channel, line)

    def _render_status_event(self, ev: StatusEvent) -> None:
        self.SetStatusText(ev.text, 0)
        self._append_to_tab(self._status_tab_name, f"*** {ev.text}\n")

    # -----------------------------------------------------------------
    # Shutdown
    # -----------------------------------------------------------------

    def _render_node_list_event(self, ev: NodeListEvent) -> None:
        self._known_nodes = ev.nodes
        self._rebuild_left_list()

        # Status strip
        self.SetStatusText(f"Nodes: {len(ev.nodes)}", 1)

    def _render_channel_list_event(self, ev: ChannelListEvent) -> None:
        self._known_channels = ev.channels
        self._rebuild_left_list()

    def _render_history_event(self, ev: HistoryEvent) -> None:
        # Render persisted history for a channel/DM.
        # Do not steal focus.
        self._ensure_tab(ev.channel, select=False)

        first_load = ev.channel not in self._history_loaded
        if first_load:
            self._history_loaded.add(ev.channel)
        if first_load:
            ctrl = self._get_text_ctrl_for_tab(ev.channel)
            if ctrl is not None:
                ctrl.SetValue("")
        for (origin_id, seqno, channel, nick, text, ts) in ev.messages:
            ts_str = time.strftime("%H:%M:%S", time.localtime(ts))
            line = f"[{ts_str}] <{nick}> {text}\n"
            self._append_to_tab(channel, line)

    def _rebuild_left_list(self) -> None:
        # Preserve selection if possible
        selected = None
        sel_idx = self.node_list.GetFirstSelected()
        if sel_idx != -1:
            selected = self.node_list.GetItemText(sel_idx)

        self.node_list.DeleteAllItems()
        self.node_list.InsertItem(0, "#general")
        row = 1

        # Local channels/DMs from history (excluding #general)
        for chan in self._known_channels:
            if chan == "#general":
                continue
            self.node_list.InsertItem(row, chan)
            row += 1

        # Discovered nodes as DMs (displayed as @CALLSIGN)
        for callsign in self._known_nodes:
            name = f"@{callsign}"
            # Avoid duplicates if already present
            if name in self._known_channels:
                continue
            self.node_list.InsertItem(row, name)
            row += 1

        if selected is not None:
            for i in range(self.node_list.GetItemCount()):
                if self.node_list.GetItemText(i) == selected:
                    self.node_list.Select(i)
                    break

    def on_close(self, _event: wx.CloseEvent) -> None:
        self._timer.Stop()
        self.backend.shutdown()
        self.Destroy()


# =====================================================================
# wx.App bootstrap
# =====================================================================

class MeshChatApp(wx.App):
    def __init__(self, backend: BackendInterface, cfg_path: str = "config.yaml", **kwargs) -> None:
        self._backend = backend
        self._config_path = cfg_path
        super().__init__(**kwargs)

    def OnInit(self) -> bool:
        frame = ChatFrame(self._backend, cfg_path=self._config_path)
        frame.Show()
        return True


# =====================================================================
# Entry point
# =====================================================================

if __name__ == "__main__":
    from pathlib import Path
    from config_loader import load_chat_config_from_yaml

    config_path = Path("config.yaml")
    chat_config = load_chat_config_from_yaml(str(config_path))

    if not chat_config.peers:
        raise RuntimeError("No peers configured in chat.peers of config.yaml")

    default_peer_nick = next(iter(chat_config.peers.keys()))

    back_end = MeshChatBackend(
        config=chat_config,
        default_peer_nick=default_peer_nick,
        status_heartbeat_interval=60.0,
    )

    app = MeshChatApp(back_end, cfg_path=str(config_path))
    app.MainLoop()
