"""
chatlogs_gui.py — wxPython dialog wrapper for chat log export

This module provides a simple UI to run the exporter:
- one file per channel/DM
- output folder: ../logs/<YYYYmmdd-HHMMSS> (UTC)
"""

from __future__ import annotations

import os
import wx

from chatlogs_export import export_all_chat_logs, _load_db_path_from_config


class ChatLogsExportDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, *, config_path: str = "config.yaml") -> None:
        super().__init__(parent, title="Export Chat Logs", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._config_path = str(config_path)

        self._out_base = os.path.abspath(os.path.join("..", "logs"))
        self._fmt = "csv"

        self._build_ui()
        self.Fit()
        self.SetSize(wx.Size(520, 260))

    def _build_ui(self) -> None:
        sizer = wx.BoxSizer(wx.VERTICAL)

        info = wx.StaticText(
            self,
            label="Exports one file per channel/DM from the local SQLite database.\n"
                  "A new timestamped folder will be created under the chosen base directory.",
        )
        sizer.Add(info, 0, wx.ALL, 10)

        # Output base folder picker
        row1 = wx.BoxSizer(wx.HORIZONTAL)
        row1.Add(wx.StaticText(self, label="Base folder:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.dir_picker = wx.DirPickerCtrl(self, path=self._out_base, message="Choose base export folder")
        row1.Add(self.dir_picker, 1, wx.EXPAND)
        sizer.Add(row1, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # Format choice
        row2 = wx.BoxSizer(wx.HORIZONTAL)
        row2.Add(wx.StaticText(self, label="Format:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.format_choice = wx.Choice(self, choices=["csv", "txt"])
        self.format_choice.SetSelection(0)
        row2.Add(self.format_choice, 0)
        sizer.Add(row2, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # Status box
        self.status = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.status.SetMinSize(wx.Size(-1, 90))
        sizer.Add(self.status, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # Buttons
        btns = wx.StdDialogButtonSizer()
        self.btn_export = wx.Button(self, wx.ID_OK, "Export Now")
        self.btn_close = wx.Button(self, wx.ID_CANCEL, "Close")
        btns.AddButton(self.btn_export)
        btns.AddButton(self.btn_close)
        btns.Realize()
        sizer.Add(btns, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        self.SetSizer(sizer)

        self.btn_export.Bind(wx.EVT_BUTTON, self._on_export)

    def _log(self, msg: str) -> None:
        self.status.AppendText(msg.rstrip() + "\n")

    def _on_export(self, _event: wx.CommandEvent) -> None:
        out_base = self.dir_picker.GetPath()
        fmt = self.format_choice.GetStringSelection() or "csv"

        self.status.SetValue("")
        self._log("Starting export...")

        try:
            db_path = _load_db_path_from_config(self._config_path)
            result = export_all_chat_logs(db_path=db_path, out_base_dir=out_base, fmt=fmt)
        except (OSError, ValueError) as exc:
            self._log(f"ERROR: {exc}")
            wx.MessageBox(str(exc), "Export Failed", wx.ICON_ERROR)
            return

        self._log(f"Export complete.")
        self._log(f"Channels discovered: {result.channels_exported}")
        self._log(f"Files written: {result.files_written}")
        self._log(f"Output folder: {result.export_dir}")

        wx.MessageBox(f"Exported {result.files_written} file(s).\n\n{result.export_dir}", "Export Complete", wx.ICON_INFORMATION)


def open_chatlogs_exporter(parent: wx.Window, *, config_path: str = "config.yaml") -> bool:
    """
    Open the export dialog. Returns True if the user ran an export successfully.
    """
    dlg = ChatLogsExportDialog(parent, config_path=config_path)
    try:
        dlg.ShowModal()
    finally:
        dlg.Destroy()
    return True
