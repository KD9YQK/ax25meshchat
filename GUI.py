#!/usr/bin/env python3
"""
Mesh Chat GUI (wxPython version)

Layout:
- Top: SplitterWindow
    - Left: Nodes / Channels list
    - Right: Tabbed chat (AuiNotebook with close buttons)
- Bottom: Input row (separate textbox + Send button) spanning full width
- Status bar at the bottom

Backend contract:
- backend: BackendInterface
    - get_ui_queue() -> queue.Queue[UIEvent]
    - send_message(channel: str, text: str) -> None
    - request_history(channel: str, limit: int = 200) -> None
    - request_sync_for_channel(channel: str) -> None
    - prune_db() -> None
    - shutdown() -> None
"""

from __future__ import annotations

import queue
import time
from typing import Optional

import wx
import wx.aui as aui

from config_loader import load_gui_theme_from_yaml, load_gui_identity_from_yaml

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


class ChatFrame(wx.Frame):
    POLL_INTERVAL_MS = 100

    def __init__(self, backend: BackendInterface, config_path: str = "config.yaml") -> None:
        super().__init__(None, title="ARDOP Mesh Chat", size=wx.Size(1000, 700))
        self.backend = backend
        self.ui_queue: queue.Queue[UIEvent] = backend.get_ui_queue()
        self._config_path: str = str(config_path)

        # GUI theme + identity (loaded from config.yaml). Safe defaults if absent.
        self._gui_theme: dict[str, object] = {}
        self._callsign: str = ""
        self._peer_nicks: set[str] = set()
        self._peer_keys: set[str] = set()
        self._theme_colors: dict[str, Optional[wx.Colour]] = {}
        self._theme_font_sizes: dict[str, int] = {}
        self._load_gui_preferences()

        self._status_tab_name = "Status"
        self._known_nodes: list[str] = []
        self._known_channels: list[str] = []
        self._history_loaded: set[str] = set()

        self._build_ui()

        # Preload local history for #general (does not steal focus)
        try:
            self.backend.request_history("#general", limit=200)
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
        # Menu bar
        # =======================
        menubar = wx.MenuBar()
        settings_menu = wx.Menu()

        mi_config = settings_menu.Append(wx.ID_ANY, "Config…", "Edit config.yaml (requires restart to apply)")
        self.Bind(wx.EVT_MENU, self.on_open_config, mi_config)

        settings_menu.AppendSeparator()

        mi_prune = settings_menu.Append(wx.ID_ANY, "Prune Chat DB…", "Delete old chat history from the local database")
        self.Bind(wx.EVT_MENU, self.on_prune_db, mi_prune)

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
        self.node_list = wx.ListCtrl(left_panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.node_list.InsertColumn(0, "Name", width=200)

        # Built-in channel(s). Dynamic channels/nodes are populated from mesh state + DB.
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
            style=(aui.AUI_NB_DEFAULT_STYLE | aui.AUI_NB_TAB_MOVE | aui.AUI_NB_CLOSE_ON_ACTIVE_TAB),
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

        # Apply theme after widgets exist.
        self._apply_gui_theme()

        # Create initial Status tab (select it at startup)
        self._ensure_tab(self._status_tab_name, select=True)

        # Apply configured colors/fonts (if any)
        self._apply_gui_theme()

    # -----------------------------------------------------------------
    # Tab helpers
    # -----------------------------------------------------------------

    def _ensure_tab(self, name: str, select: bool) -> bool:
        for idx in range(self.notebook.GetPageCount()):
            if self.notebook.GetPageText(idx) == name:
                if select:
                    self.notebook.SetSelection(idx)
                return False

        panel = wx.Panel(self.notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)

        text_ctrl = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2)
        self._apply_theme_to_chat_ctrl(text_ctrl)
        sizer.Add(text_ctrl, 1, wx.EXPAND)

        panel.SetSizer(sizer)
        self.notebook.AddPage(panel, name, select=select)
        return True

    def _create_tab(self, name: str) -> None:
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
        """Append plain (un-styled) text to a tab."""
        self._ensure_tab(name, select=False)
        ctrl = self._get_text_ctrl_for_tab(name)
        if ctrl is None:
            return
        ctrl.SetInsertionPointEnd()
        ctrl.WriteText(text)

    def _chat_text_attr(self, *, fg: Optional[wx.Colour] = None, bold: bool = False) -> wx.TextAttr:
        chat_fg = self._theme_colors.get("chat_fg")
        chat_bg = self._theme_colors.get("chat_bg")

        font = wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT)
        font.SetPointSize(self._theme_font_sizes.get("chat", font.GetPointSize()))
        if bold:
            font.SetWeight(wx.FONTWEIGHT_BOLD)

        use_fg = fg if fg is not None else chat_fg
        use_bg = chat_bg

        if use_fg is None:
            fg_out = wx.NullColour
        else:
            fg_out = use_fg
        if use_bg is None:
            bg_out = wx.NullColour
        else:
            bg_out = use_bg
        return wx.TextAttr(fg_out, bg_out, font)

    def _append_styled_parts(self, name: str, parts: list[tuple[str, wx.TextAttr]]) -> None:
        self._ensure_tab(name, select=False)
        ctrl = self._get_text_ctrl_for_tab(name)
        if ctrl is None:
            return
        ctrl.SetInsertionPointEnd()
        for text, attr in parts:
            ctrl.SetDefaultStyle(attr)
            ctrl.WriteText(text)
        # Reset to default style for future appends.
        ctrl.SetDefaultStyle(self._chat_text_attr())

    def _append_chat_line(self, tab: str, ts_str: str, nick: str, text: str, *, category: str) -> None:
        """Append a single chat line, highlighting the sender portion."""

        highlight = self._theme_colors.get(category)
        normal_attr = self._chat_text_attr()
        nick_attr = self._chat_text_attr(fg=highlight, bold=True)

        self._append_styled_parts(
            tab,
            parts=[
                (f"[{ts_str}] ", normal_attr),
                (f"<{nick}>", nick_attr),
                (f" {text}\n", normal_attr),
            ],
        )

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
        created = self._ensure_tab(name, select=True)
        self.SetStatusText(f"Active: {name}", 2)

        # Load local history when a tab is created (e.g. first open or reopen after close)
        if created and name != self._status_tab_name:
            # Forget any cached flag so HistoryEvent is treated as a first load for this tab.
            self._history_loaded.discard(name)
            try:
                self.backend.request_history(name, limit=200)
            except AttributeError:
                pass

        # Also request a network sync (backend throttles)
        try:
            self.backend.request_sync_for_channel(name)
        except AttributeError:
            pass

    def on_send(self, _event: wx.CommandEvent) -> None:
        text = self.input_box.GetValue().strip()
        if not text:
            return

        tab_name = self._get_current_tab_name()
        if tab_name == self._status_tab_name:
            tab_name = "#general"

        ts_str = time.strftime("%H:%M:%S", time.localtime())
        self._append_chat_line(tab_name, ts_str, "me", text, category="me")

        self.input_box.SetValue("")
        self.backend.send_message(tab_name, text)

    def on_tab_close(self, event: aui.AuiNotebookEvent) -> None:
        idx = event.GetSelection()
        name = self.notebook.GetPageText(idx)
        if name == self._status_tab_name:
            event.Veto()
            return
        # Allow tab close, but forget cached history so reopening reloads from DB.
        self._history_loaded.discard(name)

    def on_open_config(self, _event: wx.CommandEvent) -> None:
        # Lazy import so the main GUI can run even if the config editor module is missing.
        try:
            from config_gui import open_config_editor  # type: ignore
        except ImportError:
            wx.MessageBox(
                "Config editor module 'config_gui.py' not found.",
                "Config",
                wx.ICON_WARNING,
            )
            return

        saved = open_config_editor(self, config_path=str(self._config_path))
        if saved:
            wx.MessageBox(
                "Saved config.yaml. Restart the app to apply changes.",
                "Config",
                wx.ICON_INFORMATION,
            )

    def on_prune_db(self, _event: wx.CommandEvent) -> None:
        dlg = wx.MessageDialog(
            self,
            "This will delete old chat history from your local SQLite database.\n\n"
            "This cannot be undone.\n\n"
            "Proceed?",
            "Prune Chat DB",
            style=wx.OK | wx.CANCEL | wx.ICON_WARNING,
        )
        try:
            result = dlg.ShowModal()
        finally:
            dlg.Destroy()

        if result != wx.ID_OK:
            return

        try:
            self.backend.prune_db()
        except AttributeError:
            wx.MessageBox("Backend does not support pruning.", "Not Supported", wx.ICON_ERROR)
            return

        # Reload history for all open chat tabs to reflect pruning.
        # (Status tab is excluded; it is not backed by DB history.)
        open_tabs: list[str] = []
        for idx in range(self.notebook.GetPageCount()):
            name = self.notebook.GetPageText(idx)
            if name != self._status_tab_name:
                open_tabs.append(name)

        for name in open_tabs:
            self._history_loaded.discard(name)

        for name in open_tabs:
            try:
                self.backend.request_history(name, limit=200)
            except AttributeError:
                break

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

    # -----------------------------------------------------------------
    # Rendering helpers
    # -----------------------------------------------------------------

    def _render_chat_event(self, ev: ChatEvent) -> None:
        ts_str = time.strftime("%H:%M:%S", time.localtime(ev.timestamp))
        category = self._categorize_sender(ev.nick)
        self._append_chat_line(ev.channel, ts_str, ev.nick, ev.text, category=category)

    def _render_status_event(self, ev: StatusEvent) -> None:
        self.SetStatusText(ev.text, 0)
        self._append_to_tab(self._status_tab_name, f"*** {ev.text}\n")

    def _render_node_list_event(self, ev: NodeListEvent) -> None:
        self._known_nodes = ev.nodes
        self._rebuild_left_list()
        self.SetStatusText(f"Nodes: {len(ev.nodes)}", 1)

    def _render_channel_list_event(self, ev: ChannelListEvent) -> None:
        self._known_channels = ev.channels
        self._rebuild_left_list()

    def _render_history_event(self, ev: HistoryEvent) -> None:
        self._ensure_tab(ev.channel, select=False)

        first_load = ev.channel not in self._history_loaded
        if first_load:
            self._history_loaded.add(ev.channel)
            ctrl = self._get_text_ctrl_for_tab(ev.channel)
            if ctrl is not None:
                ctrl.SetValue("")

        for (origin_id, seqno, channel, nick, text, ts) in ev.messages:
            ts_str = time.strftime("%H:%M:%S", time.localtime(ts))
            category = self._categorize_sender(nick)
            self._append_chat_line(channel, ts_str, nick, text, category=category)

    # -----------------------------------------------------------------
    # Theme + sender categorization
    # -----------------------------------------------------------------

    def _load_gui_preferences(self) -> None:
        try:
            self._gui_theme = load_gui_theme_from_yaml(self._config_path)
        except (OSError, ValueError):
            self._gui_theme = {}

        try:
            ident = load_gui_identity_from_yaml(self._config_path)
        except (OSError, ValueError):
            ident = {"callsign": "", "peer_nicks": [], "peer_keys": []}

        self._callsign = str(ident.get("callsign", "") or "")
        self._peer_nicks = set(str(x) for x in (ident.get("peer_nicks", []) or []))
        self._peer_keys = set(str(x) for x in (ident.get("peer_keys", []) or []))

        self._theme_colors = self._parse_theme_colors(self._gui_theme.get("colors"))
        self._theme_font_sizes = self._parse_theme_font_sizes(self._gui_theme.get("font_sizes"))

    @staticmethod
    def _parse_theme_colors(colors_any: object) -> dict[str, Optional[wx.Colour]]:
        def _to_colour(v: object) -> Optional[wx.Colour]:
            if not isinstance(v, str):
                return None
            raw = v.strip()
            if not raw:
                return None
            if raw.startswith("#") and len(raw) == 7:
                try:
                    r = int(raw[1:3], 16)
                    g = int(raw[3:5], 16)
                    b = int(raw[5:7], 16)
                except ValueError:
                    return None
                return wx.Colour(r, g, b)
            return None

        colors: dict[str, Optional[wx.Colour]] = {
            "window_bg": None,
            "chat_bg": None,
            "chat_fg": None,
            "input_bg": None,
            "input_fg": None,
            "list_bg": None,
            "list_fg": None,
            "status_bg": None,
            "status_fg": None,
            "me": None,
            "known": None,
            "unknown": None,
        }

        if not isinstance(colors_any, dict):
            return colors

        for k in list(colors.keys()):
            colors[k] = _to_colour(colors_any.get(k))

        return colors

    @staticmethod
    def _parse_theme_font_sizes(fonts_any: object) -> dict[str, int]:
        sizes = {
            "chat": 10,
            "input": 10,
            "list": 10,
            "status": 10,
        }
        if not isinstance(fonts_any, dict):
            return sizes
        for k in list(sizes.keys()):
            val = fonts_any.get(k)
            if isinstance(val, int) and 6 <= val <= 48:
                sizes[k] = val
        return sizes

    def _categorize_sender(self, nick: str) -> str:
        n = nick.strip()
        if not n:
            return "unknown"
        if n == "me" or (self._callsign and n == self._callsign):
            return "me"
        if n in self._peer_nicks or n in self._peer_keys:
            return "known"
        if n in self._known_nodes:
            return "known"
        return "unknown"

    def _apply_gui_theme(self) -> None:
        # Window/panels
        window_bg = self._theme_colors.get("window_bg")
        if window_bg is not None:
            self.SetBackgroundColour(window_bg)

        # Left list
        list_bg = self._theme_colors.get("list_bg")
        list_fg = self._theme_colors.get("list_fg")
        if list_bg is not None:
            self.node_list.SetBackgroundColour(list_bg)
        if list_fg is not None:
            self.node_list.SetForegroundColour(list_fg)
        self._apply_font_size(self.node_list, self._theme_font_sizes.get("list", 10))

        # Input
        input_bg = self._theme_colors.get("input_bg")
        input_fg = self._theme_colors.get("input_fg")
        if input_bg is not None:
            self.input_box.SetBackgroundColour(input_bg)
        if input_fg is not None:
            self.input_box.SetForegroundColour(input_fg)
        self._apply_font_size(self.input_box, self._theme_font_sizes.get("input", 10))
        self._apply_font_size(self.send_button, self._theme_font_sizes.get("input", 10))

        # Existing tabs
        for idx in range(self.notebook.GetPageCount()):
            name = self.notebook.GetPageText(idx)
            ctrl = self._get_text_ctrl_for_tab(name)
            if ctrl is not None:
                self._apply_theme_to_chat_ctrl(ctrl)

        self.Refresh()

    def _apply_theme_to_chat_ctrl(self, ctrl: wx.TextCtrl) -> None:
        chat_bg = self._theme_colors.get("chat_bg")
        chat_fg = self._theme_colors.get("chat_fg")
        if chat_bg is not None:
            ctrl.SetBackgroundColour(chat_bg)
        if chat_fg is not None:
            ctrl.SetForegroundColour(chat_fg)
        self._apply_font_size(ctrl, self._theme_font_sizes.get("chat", 10))

    @staticmethod
    def _apply_font_size(win: wx.Window, point_size: int) -> None:
        font = win.GetFont()
        if font.IsOk():
            font.SetPointSize(point_size)
            win.SetFont(font)

    def _rebuild_left_list(self) -> None:
        selected = None
        sel_idx = self.node_list.GetFirstSelected()
        if sel_idx != -1:
            selected = self.node_list.GetItemText(sel_idx)

        self.node_list.DeleteAllItems()
        self.node_list.InsertItem(0, "#general")
        row = 1

        for chan in self._known_channels:
            if chan == "#general":
                continue
            self.node_list.InsertItem(row, chan)
            row += 1

        for callsign in self._known_nodes:
            name = f"@{callsign}"
            if name in self._known_channels:
                continue
            self.node_list.InsertItem(row, name)
            row += 1

        if selected is not None:
            for i in range(self.node_list.GetItemCount()):
                if self.node_list.GetItemText(i) == selected:
                    self.node_list.Select(i)
                    break

    # -----------------------------------------------------------------
    # Shutdown
    # -----------------------------------------------------------------

    def on_close(self, _event: wx.CloseEvent) -> None:
        self._timer.Stop()
        self.backend.shutdown()
        self.Destroy()


class MeshChatApp(wx.App):
    def __init__(self, backend: BackendInterface, config_path: str = "config.yaml", **kwargs) -> None:
        self._backend = backend
        self._config_path: str = str(config_path)
        super().__init__(**kwargs)

    def OnInit(self) -> bool:
        frame = ChatFrame(self._backend, config_path=self._config_path)
        frame.Show()
        return True


if __name__ == "__main__":
    from pathlib import Path
    from config_loader import load_chat_config_from_yaml

    _config_path = Path("config.yaml")
    chat_config = load_chat_config_from_yaml(str(_config_path))

    if not chat_config.peers:
        raise RuntimeError("No peers configured in chat.peers of config.yaml")

    default_peer_nick = next(iter(chat_config.peers.keys()))

    back_end = MeshChatBackend(
        config=chat_config,
        default_peer_nick=default_peer_nick,
        status_heartbeat_interval=60.0,
    )

    app = MeshChatApp(back_end, config_path=str(_config_path))
    app.MainLoop()
