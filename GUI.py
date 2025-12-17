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
import threading
import time
from typing import Optional

import wx
import wx.aui as aui

from mesh_backend import (
    BackendInterface,
    ChatEvent,
    StatusEvent,
    UIEvent,
    MeshChatBackend,
)


# =====================================================================
# Simple FakeBackend for local testing (no RF, no mesh)
# =====================================================================

class FakeBackend(BackendInterface):
    """
    Simple in-memory backend that:

    - Accepts send_message() calls.
    - Periodically emits StatusEvent heartbeats.
    """

    def __init__(self, nick: str = "Me", default_channel: str = "#general") -> None:
        self._nick = nick
        self._default_channel = default_channel
        self._ui_queue: queue.Queue[UIEvent] = queue.Queue()
        self._running = True

        self._status_thread = threading.Thread(
            target=self._status_loop,
            name="FakeBackendStatus",
            daemon=True,
        )
        self._status_thread.start()

    def get_ui_queue(self) -> queue.Queue[UIEvent]:
        return self._ui_queue

    def send_message(self, channel: str, text: str) -> None:
        # For fake backend, just log to status so you see activity.
        self._ui_queue.put(StatusEvent(text=f"Fake send to {channel}: {text!r}"))

    def shutdown(self) -> None:
        self._running = False

    def _status_loop(self) -> None:
        while self._running:
            time.sleep(10.0)
            if not self._running:
                return
            self._ui_queue.put(StatusEvent(text="Fake backend heartbeat..."))


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

    def __init__(self, backend: BackendInterface) -> None:
        super().__init__(None, title="ARDOP Mesh Chat", size=wx.Size(1000, 700))
        self.backend = backend
        self.ui_queue: queue.Queue[UIEvent] = backend.get_ui_queue()
        self._status_tab_name = "Status"

        self._build_ui()

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

        # Demo entries; real app will populate from mesh/chat state
        self.node_list.InsertItem(0, "#general")
        self.node_list.InsertItem(1, "#random")
        self.node_list.InsertItem(2, "@K0TEST-7")

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
        self.SetStatusText("Ready")

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
        except queue.Empty:
            pass

    # -----------------------------------------------------------------
    # Rendering helpers
    # -----------------------------------------------------------------

    def _render_chat_event(self, ev: ChatEvent) -> None:
        ts_str = time.strftime("%H:%M:%S", time.localtime(ev.timestamp))
        line = f"[{ts_str}] <{ev.nick}> {ev.text}\n"
        self._append_to_tab(ev.channel, line)

    def _render_status_event(self, ev: StatusEvent) -> None:
        self.SetStatusText(ev.text)
        self._append_to_tab(self._status_tab_name, f"*** {ev.text}\n")

    # -----------------------------------------------------------------
    # Shutdown
    # -----------------------------------------------------------------

    def on_close(self, _event: wx.CloseEvent) -> None:
        self._timer.Stop()
        self.backend.shutdown()
        self.Destroy()


# =====================================================================
# wx.App bootstrap
# =====================================================================

class MeshChatApp(wx.App):
    def __init__(self, backend: BackendInterface, **kwargs) -> None:
        self._backend = backend
        super().__init__(**kwargs)

    def OnInit(self) -> bool:
        frame = ChatFrame(self._backend)
        frame.Show()
        return True


# =====================================================================
# Entry point
# =====================================================================

if __name__ == "__main__":
    testing = False  # flip to False to use real MeshChatBackend

    if testing:
        back_end: BackendInterface = FakeBackend(
            nick="Tester",
            default_channel="#general",
        )
    else:
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

    app = MeshChatApp(back_end)
    app.MainLoop()
