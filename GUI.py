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
        # GUI theme + identity (from config.yaml). Loaded here so the GUI can
        # display local-echo lines with the correct callsign and apply optional theming.
        self._gui_theme: dict = {}
        self._local_callsign: str = ""
        self._known_peer_nicks: set[str] = set()
        self._load_gui_identity_and_theme()

        self._status_tab_name = "Status"
        self._known_nodes: list[str] = []
        self._known_channels: list[str] = []
        self._history_loaded: set[str] = set()

        self._build_ui()

        # Apply theme (colors/fonts) after widgets are constructed.
        self._apply_gui_theme()

        # Preload local history for #general (does not steal focus)
        try:
            self.backend.request_history("#general", limit=0)
        except AttributeError:
            pass

        # Timer for backend queue polling
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_timer, self._timer)
        self._timer.Start(self.POLL_INTERVAL_MS)

        self.Bind(wx.EVT_CLOSE, self.on_close)

    # -----------------------------------------------------------------
    # Theme / identity helpers
    # -----------------------------------------------------------------

    def _load_gui_identity_and_theme(self) -> None:
        """Load gui theme + identity from YAML via config_loader (GUI-only)."""
        try:
            from config_loader import load_gui_identity_from_yaml, load_gui_theme_from_yaml  # type: ignore
            ident = load_gui_identity_from_yaml(str(self._config_path))
            theme = load_gui_theme_from_yaml(str(self._config_path))
        except (ImportError, OSError, ValueError, TypeError, KeyError):
            ident = {"callsign": "", "peer_nicks": [], "peer_keys": []}
            theme = {}

        callsign = str(ident.get("callsign", "") or "").strip()
        self._local_callsign = callsign
        try:
            self._known_peer_nicks = set(str(x) for x in (ident.get("peer_nicks", []) or []) if str(x).strip())
        except (TypeError, ValueError):
            self._known_peer_nicks = set()

        self._gui_theme = theme if isinstance(theme, dict) else {}

    @staticmethod
    def _parse_hex_color(s: str) -> Optional[wx.Colour]:
        """Parse '#RRGGBB' into wx.Colour. Returns None on failure."""
        s = (s or "").strip()
        if not s:
            return None
        if s.startswith("#"):
            s = s[1:]
        if len(s) != 6:
            return None
        try:
            r = int(s[0:2], 16)
            g = int(s[2:4], 16)
            b = int(s[4:6], 16)
        except ValueError:
            return None
        return wx.Colour(r, g, b)

    def _theme_get_color(self, path: str) -> Optional[wx.Colour]:
        # path like 'colors.chat_bg'
        parts = path.split(".")
        cur: object = self._gui_theme
        for p in parts:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(p)
        if isinstance(cur, str):
            return self._parse_hex_color(cur)
        return None

    def _theme_get_font_size(self, key: str, default: int) -> int:
        fs = self._gui_theme.get("font_sizes") if isinstance(self._gui_theme, dict) else None
        if isinstance(fs, dict):
            try:
                val = int(fs.get(key, default))
                if val < 6:
                    val = 6
                if val > 32:
                    val = 32
                return val
            except (TypeError, ValueError):
                return default
        return default

    def _apply_gui_theme(self) -> None:
        """Apply optional theme colors/fonts to GUI widgets."""
        # Colors
        window_bg = self._theme_get_color("colors.window_bg")
        list_bg = self._theme_get_color("colors.list_bg")
        list_fg = self._theme_get_color("colors.list_fg")
        input_bg = self._theme_get_color("colors.input_bg")
        input_fg = self._theme_get_color("colors.input_fg")
        chat_bg = self._theme_get_color("colors.chat_bg")
        chat_fg = self._theme_get_color("colors.chat_fg")

        # Fonts
        chat_sz = self._theme_get_font_size("chat", 10)
        input_sz = self._theme_get_font_size("input", 10)
        list_sz = self._theme_get_font_size("list", 10)

        try:
            if window_bg is not None:
                self.SetBackgroundColour(window_bg)
        except (AttributeError, TypeError, ValueError):
            pass

        try:
            if list_bg is not None:
                self.node_list.SetBackgroundColour(list_bg)
            if list_fg is not None:
                self.node_list.SetTextColour(list_fg)
            lf = self.node_list.GetFont()
            lf.SetPointSize(list_sz)
            self.node_list.SetFont(lf)
        except (AttributeError, TypeError, ValueError):
            pass

        try:
            if input_bg is not None:
                self.input_box.SetBackgroundColour(input_bg)
            if input_fg is not None:
                self.input_box.SetForegroundColour(input_fg)
            inf = self.input_box.GetFont()
            inf.SetPointSize(input_sz)
            self.input_box.SetFont(inf)
        except (AttributeError, TypeError, ValueError):
            pass

        # Apply chat font/colors to all existing chat text controls
        try:
            chat_font = self.GetFont()
            chat_font.SetPointSize(chat_sz)
        except (AttributeError, TypeError, ValueError):
            chat_font = None

        for idx in range(self.notebook.GetPageCount()):
            page = self.notebook.GetPage(idx)
            sizer = page.GetSizer()
            if not sizer or sizer.GetItemCount() == 0:
                continue
            item = sizer.GetItem(0)
            ctrl = item.GetWindow()
            if isinstance(ctrl, wx.TextCtrl):
                try:
                    if chat_bg is not None:
                        ctrl.SetBackgroundColour(chat_bg)
                    if chat_fg is not None:
                        ctrl.SetForegroundColour(chat_fg)
                    if chat_font is not None:
                        ctrl.SetFont(chat_font)
                except (AttributeError, TypeError, ValueError):
                    pass

    def _chat_style_for_nick(self, nick: str, is_me: bool) -> Optional[wx.Colour]:
        if is_me:
            col = self._theme_get_color("colors.me")
            return col
        if nick in self._known_peer_nicks:
            return self._theme_get_color("colors.known")
        return self._theme_get_color("colors.unknown")

    def _append_chat_line(self, channel: str, ts: float, nick: str, text: str, is_me: bool) -> None:
        """Append a single chat line with basic coloring (timestamp/nick)."""
        self._ensure_tab(channel, select=False)
        ctrl = self._get_text_ctrl_for_tab(channel)
        if ctrl is None:
            return
        ts_str = time.strftime("%H:%M:%S", time.localtime(ts))
        base_fg = self._theme_get_color("colors.chat_fg")
        if base_fg is None:
            base_fg = ctrl.GetForegroundColour()
        ts_col = base_fg if isinstance(base_fg, wx.Colour) else None
        nick_col = self._chat_style_for_nick(nick, is_me=is_me)

        ctrl.SetInsertionPointEnd()

        # Timestamp
        if ts_col is not None:
            ctrl.SetDefaultStyle(wx.TextAttr(ts_col))
        ctrl.WriteText(f"[{ts_str}] ")

        # Nick
        if nick_col is not None:
            ctrl.SetDefaultStyle(wx.TextAttr(nick_col))
        ctrl.WriteText(f"<{nick}> ")

        # Message text
        if base_fg is not None:
            ctrl.SetDefaultStyle(wx.TextAttr(base_fg))
        ctrl.WriteText(f"{text}\n")

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

        settings_menu.AppendSeparator()

        mi_export = settings_menu.Append(wx.ID_ANY, "Export Chat Logs…", "Export all channels/DMs to files")
        self.Bind(wx.EVT_MENU, self.on_export_chat_logs, mi_export)

        menubar.Append(settings_menu, "Settings")
        self.SetMenuBar(menubar)

        # =======================
        # Top: Splitter
        # =======================
        splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)

        # Left panel: nodes / channels
        left_panel = wx.Panel(splitter)
        left_sizer = wx.BoxSizer(wx.VERTICAL)

        self.node_list = wx.ListCtrl(
            left_panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.LC_NO_HEADER,
        )
        # Single hidden header column; LC_NO_HEADER removes the visible header bar.
        self.node_list.InsertColumn(0, "", width=200)

        # Built-in channel(s). Dynamic channels/nodes are populated from mesh state + DB.
        self.node_list.InsertItem(0, "#general")

        self.node_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_node_activated)
        left_sizer.Add(self.node_list, 1, wx.EXPAND)
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

        # Create initial Status tab (select it at startup)
        self._ensure_tab(self._status_tab_name, select=True)

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
        # Apply chat theme to new tab control (colors/fonts).
        chat_bg = self._theme_get_color("colors.chat_bg")
        chat_fg = self._theme_get_color("colors.chat_fg")
        chat_sz = self._theme_get_font_size("chat", 10)
        try:
            if chat_bg is not None:
                text_ctrl.SetBackgroundColour(chat_bg)
            if chat_fg is not None:
                text_ctrl.SetForegroundColour(chat_fg)
            f = text_ctrl.GetFont()
            f.SetPointSize(chat_sz)
            text_ctrl.SetFont(f)
        except (AttributeError, TypeError, ValueError):
            pass
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
        created = self._ensure_tab(name, select=True)
        self.SetStatusText(f"Active: {name}", 2)

        # Load local history when a tab is created (e.g. first open or reopen after close)
        if created and name != self._status_tab_name:
            # Forget any cached flag so HistoryEvent is treated as a first load for this tab.
            self._history_loaded.discard(name)
            try:
                self.backend.request_history(name, limit=0)
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

        now_ts = time.time()
        nick = self._local_callsign if self._local_callsign else "me"
        self._append_chat_line(tab_name, now_ts, nick, text, is_me=True)

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
                self.backend.request_history(name, limit=0)
            except AttributeError:
                break

    def on_export_chat_logs(self, _event: wx.CommandEvent) -> None:
        # Lazy import so GUI can run without exporter files present.
        try:
            from chatlogs_gui import open_chatlogs_exporter  # type: ignore
        except ImportError:
            wx.MessageBox(
                "Chat log exporter module 'chatlogs_gui.py' not found.",
                "Export Chat Logs",
                wx.ICON_WARNING,
            )
            return

        try:
            open_chatlogs_exporter(self, config_path=str(self._config_path))
        except (OSError, ValueError) as exc:
            wx.MessageBox(str(exc), "Export Failed", wx.ICON_ERROR)

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
        is_me = bool(self._local_callsign) and (ev.nick == self._local_callsign)
        self._append_chat_line(ev.channel, ev.timestamp, ev.nick, ev.text, is_me=is_me)

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

        # HistoryEvent is treated as a full snapshot for the tab.
        self._history_loaded.add(ev.channel)

        ctrl = self._get_text_ctrl_for_tab(ev.channel)
        if ctrl is None:
            return

        # Bulk-ish update: freeze to avoid repaints, but keep rich styling.
        try:
            ctrl.Freeze()
        except AttributeError:
            pass

        ctrl.SetValue("")

        base_fg = self._theme_get_color("colors.chat_fg")
        if base_fg is None:
            base_fg = ctrl.GetForegroundColour()
        ts_col = base_fg if isinstance(base_fg, wx.Colour) else None

        for (_origin_id, _seqno, channel, nick, text, ts) in ev.messages:
            is_me = bool(self._local_callsign) and (nick == self._local_callsign)
            nick_col = self._chat_style_for_nick(nick, is_me=is_me)
            ts_str = time.strftime("%H:%M:%S", time.localtime(ts))

            ctrl.SetInsertionPointEnd()
            if ts_col is not None:
                ctrl.SetDefaultStyle(wx.TextAttr(ts_col))
            ctrl.WriteText(f"[{ts_str}] ")

            if nick_col is not None:
                ctrl.SetDefaultStyle(wx.TextAttr(nick_col))
            ctrl.WriteText(f"<{nick}> ")

            if base_fg is not None:
                ctrl.SetDefaultStyle(wx.TextAttr(base_fg))
            ctrl.WriteText(f"{text}\n")

        try:
            ctrl.Thaw()
        except AttributeError:
            pass

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
