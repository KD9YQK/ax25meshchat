#!/usr/bin/env python3
"""
Chat log exporter for ARDOP Mesh Chat.

- Exports one file per channel/DM from the SQLite chat store.
- Creates a timestamped folder under a base logs directory.

This module is designed to be:
- importable (for GUI or other code)
- runnable as a standalone script
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

import yaml

from chat_store import ChatStore


@dataclass(frozen=True)
class ExportResult:
    export_dir: str
    files_written: int
    channels_exported: int


def _timestamped_folder_name(now: Optional[float] = None) -> str:
    if now is None:
        now = time.time()
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    # UTC folder name, deterministic & filesystem-friendly
    return dt.strftime("%Y%m%d-%H%M%S")


_filename_safe_re = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_channel_to_filename(channel: str, ext: str) -> str:
    ch = channel.strip()
    if ch.startswith("#"):
        stem = ch[1:] or "channel"
    elif ch.startswith("@"):
        stem = f"DM_{ch[1:] or 'unknown'}"
    else:
        stem = ch or "channel"

    stem = _filename_safe_re.sub("_", stem)
    stem = stem.strip("._-")
    if not stem:
        stem = "channel"
    return f"{stem}.{ext}"


def _load_db_path_from_config(config_path: str) -> str:
    # Resolve db_path relative to the config file directory.
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    chat_cfg = cfg.get("chat") or {}
    db_path = chat_cfg.get("db_path")
    if not isinstance(db_path, str) or not db_path.strip():
        raise ValueError("config.yaml missing chat.db_path")
    db_path = db_path.strip()

    base_dir = os.path.dirname(os.path.abspath(config_path))
    if os.path.isabs(db_path):
        return db_path
    return os.path.abspath(os.path.join(base_dir, db_path))


def export_all_chat_logs(
    *,
    db_path: str,
    out_base_dir: str = os.path.join("..", "logs"),
    export_dir_name: Optional[str] = None,
    fmt: str = "csv",
) -> ExportResult:
    """
    Export all channels/DMs from the chat SQLite DB.

    Args:
        db_path: Path to SQLite DB (chat.db_path).
        out_base_dir: Base folder under which a timestamped export folder is created.
        export_dir_name: Optional folder name override. If None, uses UTC timestamp.
        fmt: "csv" or "txt".

    Returns:
        ExportResult with directory and counts.
    """
    fmt_norm = fmt.strip().lower()
    if fmt_norm not in ("csv", "txt"):
        raise ValueError("fmt must be 'csv' or 'txt'")

    export_dir = export_dir_name or _timestamped_folder_name()
    out_dir = os.path.abspath(os.path.join(out_base_dir, export_dir))
    os.makedirs(out_dir, exist_ok=True)

    store = ChatStore(db_path)
    try:
        channels = store.list_channels(limit=100000)
        files_written = 0

        for ch in channels:
            # Export all messages for that channel
            rows = store.get_recent_messages(ch, limit=0)
            if not rows:
                continue

            filename = _sanitize_channel_to_filename(ch, fmt_norm)
            out_path = os.path.join(out_dir, filename)

            if fmt_norm == "csv":
                _write_channel_csv(out_path, ch, rows)
            else:
                _write_channel_txt(out_path, ch, rows)

            files_written += 1

    finally:
        store.close()

    return ExportResult(export_dir=out_dir, files_written=files_written, channels_exported=len(channels))


def _ts_to_iso(ts: float) -> str:
    # created_ts is stored in seconds (int) but provided as float for UI formatting
    dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    return dt.isoformat(timespec="seconds")


def _write_channel_csv(out_path: str, channel: str, rows: Iterable[tuple[bytes, int, str, str, str, float]]) -> None:
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        # Required fields: timestamps, nick, channel, message text
        w.writerow(["created_ts_iso_utc", "created_ts_unix", "nick", "channel", "text"])
        for (_origin_id, _seqno, _ch, nick, text, created_ts) in rows:
            w.writerow([_ts_to_iso(created_ts), int(created_ts), nick, channel, text])


def _write_channel_txt(out_path: str, channel: str, rows: Iterable[tuple[bytes, int, str, str, str, float]]) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"# Export: {channel}\n")
        f.write("# Format: [HH:MM:SS] <nick> message\n")
        f.write("# Times are UTC created timestamps.\n\n")
        for (_origin_id, _seqno, _ch, nick, text, created_ts) in rows:
            hhmmss = datetime.fromtimestamp(float(created_ts), tz=timezone.utc).strftime("%H:%M:%S")
            f.write(f"[{hhmmss}] <{nick}> {text}\n")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Export all mesh chat logs from SQLite.")
    p.add_argument("--config", default="config.yaml", help="Path to config.yaml (default: config.yaml)")
    p.add_argument("--db", default="", help="Override DB path (otherwise uses chat.db_path from config)")
    p.add_argument("--out-base", default=os.path.join("..", "logs"), help="Base output folder (default: ../logs)")
    p.add_argument("--format", default="csv", choices=["csv", "txt"], help="Export format (csv or txt)")
    args = p.parse_args(argv)

    try:
        if args.db.strip():
            db_path = os.path.abspath(args.db.strip())
        else:
            db_path = _load_db_path_from_config(args.config)

        result = export_all_chat_logs(db_path=db_path, out_base_dir=args.out_base, fmt=args.format)
        print(f"Exported {result.files_written} file(s) to: {result.export_dir}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
