#!/usr/bin/env python3
"""
Tkinter mesh chat GUI with IRC-style tabs.

- Uses ttk.Notebook for tabs:
    - First tab: "Status"
    - Additional tabs: one per chat channel (#general, #random, ...)
- BackendInterface is satisfied either by:
    - FakeBackend (for testing)
    - MeshChatBackend (real RF mesh via MeshChatClient)
"""

from __future__ import annotations

import queue
import threading
import time

import tkinter as tk
from tkinter import ttk

# If mesh_backend.py is in same directory:
from mesh_backend import (
    BackendInterface,
    ChatEvent,
    StatusEvent,
    UIEvent,
)


# ============================================================
# Optional local FakeBackend for offline testing
# ============================================================

class FakeBackend(BackendInterface):
    """
    Simple fake backend:

    - Echoes sent messages back into the UI queue.
    - Periodically emits a StatusEvent.
    """

    def __init__(self, nick: str = "Me", default_channel: str = "#general") -> None:
        self.nick = nick
        self.default_channel = default_channel
        self._ui_queue: queue.Queue[UIEvent] = queue.Queue()
        self._running = True
        self._status_thread = threading.Thread(
            target=self._status_loop,
            daemon=True,
        )
        self._status_thread.start()

    def get_ui_queue(self) -> queue.Queue[UIEvent]:
        return self._ui_queue

    def send_message(self, channel: str, text: str) -> None:
        ts = time.time()
        event = ChatEvent(
            channel=channel,
            nick=self.nick,
            text=text,
            timestamp=ts,
            origin_id=b"FAKE_ORIGIN",
        )
        self._ui_queue.put(event)

    def _status_loop(self) -> None:
        while self._running:
            time.sleep(30.0)
            if not self._running:
                return
            self._ui_queue.put(StatusEvent(text="Fake mesh backend heartbeat..."))

    def shutdown(self) -> None:
        self._running = False


# ============================================================
# Tkinter GUI with tabs
# ============================================================

class ChatUI(ttk.Frame):
    """
    Main chat UI frame.

    Layout:
        [ Notebook Tabs: Status | #general | #random | ... ]
        [ text area (per tab) + scrollbar ]
        [ input entry | Send button ]
        [ status bar ]
    """

    POLL_INTERVAL_MS = 100
    STATUS_TAB_NAME = "Status"

    def __init__(
            self,
            master: tk.Misc,
            backend: BackendInterface,
            initial_channels: list[str] | None = None,
            default_channel: str = "#general",
            **kwargs,
    ) -> None:
        super().__init__(master, **kwargs)
        self.backend = backend
        self.ui_queue = backend.get_ui_queue()

        if initial_channels is None:
            initial_channels = [default_channel, "#random"]

        self.default_channel = default_channel
        self.channels = list(initial_channels)

        # Map channel name -> Text widget
        self.text_widgets: dict[str, tk.Text] = {}

        self._build_widgets()
        self._layout_widgets()

        # Start polling queue
        self.after(self.POLL_INTERVAL_MS, self._poll_ui_queue)

    # ------------------------------
    # Widget construction
    # ------------------------------

    def _build_widgets(self) -> None:
        # Notebook for tabs
        self.notebook = ttk.Notebook(self)

        # Status tab
        self.status_frame = ttk.Frame(self.notebook)
        status_text = self._create_text_widget(self.status_frame)
        self.text_widgets[self.STATUS_TAB_NAME] = status_text
        self.notebook.add(self.status_frame, text=self.STATUS_TAB_NAME)

        # Channel tabs
        for ch in self.channels:
            self._create_channel_tab(ch)

        # Input area
        self.input_frame = ttk.Frame(self)
        self.input_var = tk.StringVar()
        self.input_entry = ttk.Entry(self.input_frame, textvariable=self.input_var)
        self.send_button = ttk.Button(
            self.input_frame,
            text="Send",
            command=self._on_send_clicked,
        )
        self.input_entry.bind("<Return>", self._on_enter_pressed)

        # Status bar
        self.status_var = tk.StringVar(value="Disconnected")
        self.status_label = ttk.Label(self, textvariable=self.status_var, anchor="w")

    def _layout_widgets(self) -> None:
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        # Notebook
        self.notebook.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)

        # Input area
        self.input_frame.grid(row=1, column=0, sticky="ew", padx=5, pady=(0, 5))
        self.input_frame.columnconfigure(0, weight=1)
        self.input_entry.grid(row=0, column=0, sticky="ew")
        self.send_button.grid(row=0, column=1, padx=(5, 0))

        # Status bar
        self.status_label.grid(row=2, column=0, sticky="ew", padx=5, pady=(0, 5))

    # ------------------------------
    # Tab & text helpers
    # ------------------------------

    @staticmethod
    def _create_text_widget(parent: ttk.Frame) -> tk.Text:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)

        text = tk.Text(
            frame,
            wrap="word",
            height=20,
            state="disabled",
        )
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)

        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        return text

    def _create_channel_tab(self, channel: str) -> None:
        if channel in self.text_widgets:
            return

        frame = ttk.Frame(self.notebook)
        text_widget = self._create_text_widget(frame)

        self.text_widgets[channel] = text_widget
        self.notebook.add(frame, text=channel)

    def _get_text_for_channel(self, channel: str) -> tk.Text:
        if channel not in self.text_widgets:
            self._create_channel_tab(channel)
        return self.text_widgets[channel]

    def _get_current_tab_name(self) -> str:
        current = self.notebook.select()
        tab_text = self.notebook.tab(current, "text")
        return str(tab_text)

    def _get_send_channel(self) -> str:
        tab_name = self._get_current_tab_name()
        if tab_name == self.STATUS_TAB_NAME:
            return self.default_channel
        return tab_name

    # ------------------------------
    # Event handlers
    # ------------------------------

    def _on_send_clicked(self) -> None:
        text = self.input_var.get().strip()
        if not text:
            return

        channel = self._get_send_channel()
        self.backend.send_message(channel, text)
        self.input_var.set("")

    def _on_enter_pressed(self, _event: tk.Event) -> str:
        self._on_send_clicked()
        return "break"

    # ------------------------------
    # Queue polling
    # ------------------------------

    def _poll_ui_queue(self) -> None:
        processed = False
        try:
            while True:
                event = self.ui_queue.get_nowait()
                processed = True
                if isinstance(event, ChatEvent):
                    self._render_chat_event(event)
                elif isinstance(event, StatusEvent):
                    self._render_status_event(event)
        except queue.Empty:
            if processed:
                self._auto_scroll_active_tab()
        self.after(self.POLL_INTERVAL_MS, self._poll_ui_queue)

    # ------------------------------
    # Rendering
    # ------------------------------

    def _render_chat_event(self, event: ChatEvent) -> None:
        text_widget = self._get_text_for_channel(event.channel)

        ts_struct = time.localtime(event.timestamp)
        ts_str = time.strftime("%H:%M:%S", ts_struct)
        line = f"[{ts_str}] <{event.nick}> {event.text}\n"

        text_widget.configure(state="normal")
        text_widget.insert("end", line)
        text_widget.configure(state="disabled")

        current_tab = self._get_current_tab_name()
        if current_tab == event.channel:
            text_widget.see("end")

    def _render_status_event(self, event: StatusEvent) -> None:
        self.status_var.set(event.text)

        text_widget = self.text_widgets[self.STATUS_TAB_NAME]
        text_widget.configure(state="normal")
        text_widget.insert("end", f"*** {event.text}\n")
        text_widget.configure(state="disabled")

        current_tab = self._get_current_tab_name()
        if current_tab == self.STATUS_TAB_NAME:
            text_widget.see("end")

    def _auto_scroll_active_tab(self) -> None:
        tab_name = self._get_current_tab_name()
        widget = self.text_widgets.get(tab_name)
        if widget is not None:
            widget.see("end")


class ChatApp(tk.Tk):
    def __init__(self, backend: BackendInterface) -> None:
        super().__init__()

        self.backend = backend

        self.title("Mesh Chat")
        self.geometry("900x600")

        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self.chat_ui = ChatUI(
            self,
            backend=self.backend,
            initial_channels=["#general", "#random"],
            default_channel="#general",
        )
        self.chat_ui.pack(fill="both", expand=True)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Debug: dump threads to console a couple seconds after startup
        self.after(2000, self._debug_dump_threads)

    def _on_close(self) -> None:
        self.backend.shutdown()
        self.destroy()

    @staticmethod
    def _debug_dump_threads() -> None:
        threads = threading.enumerate()
        print("=== Active threads ===")
        for t in threads:
            print(f"- {t.name!r} (daemon={t.daemon})")


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    # --- Option A: run with FakeBackend (no RF needed) ---
    back_end: BackendInterface = FakeBackend(nick="Bob", default_channel="#general")

    # --- Option B: run with real MeshChatBackend ---
    # from chat_client import MeshChatConfig, ChatPeer
    # from mesh_config import MeshNodeConfig
    #
    # mesh_cfg = MeshNodeConfig(
    #     # fill in your existing MeshNodeConfig fields here
    # )
    #
    # peers = {
    #     "BASE": ChatPeer(
    #         node_id=bytes.fromhex("0011223344556677"),
    #         nick="BASE",
    #     ),
    # }
    #
    # chat_config = MeshChatConfig(
    #     mesh_node_config=mesh_cfg,
    #     db_path="mesh_chat.db",
    #     peers=peers,
    # )
    #
    # back_end = MeshChatBackend(
    #     config=chat_config,
    #     default_peer_nick="BASE",
    #     status_heartbeat_interval=10.0,
    # )

    app = ChatApp(backend=back_end)
    app.mainloop()
