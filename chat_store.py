# chat_store.py

from __future__ import annotations

import sqlite3
import time
from typing import List, Tuple, Optional, Callable, Dict, Any


class ChatStore:
    """
    Persistent chat log using SQLite.

    - One DB file per node (configurable via path).
    - Deduplicates messages by (origin_id, seqno).
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        # Optional local-only hook: called after a message is successfully stored.
        self._on_message_stored: Optional[Callable[[Dict[str, Any]], None]] = None
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def set_on_message_stored(self, cb: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        """Set a callback invoked after add_message stores a new row.

        Callback receives a dict with existing fields only.
        """
        self._on_message_stored = cb

    def _init_schema(self) -> None:
        create_sql = """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin_id BLOB NOT NULL,
            seqno INTEGER NOT NULL,
            channel TEXT NOT NULL,
            nick TEXT NOT NULL,
            text TEXT NOT NULL,
            ts REAL NOT NULL,
            created_ts INTEGER NOT NULL,
            UNIQUE(origin_id, seqno)
        );
        """
        self._conn.execute(create_sql)

        # Schema migration: add created_ts if upgrading from older DBs.
        # For legacy rows, default created_ts to receive timestamp (ts) rounded to seconds.
        try:
            cols = [r[1] for r in self._conn.execute("PRAGMA table_info(chat_messages);").fetchall()]
        except sqlite3.Error:
            cols = []
        if "created_ts" not in cols:
            self._conn.execute("ALTER TABLE chat_messages ADD COLUMN created_ts INTEGER;")
            self._conn.execute("UPDATE chat_messages SET created_ts = CAST(ts AS INTEGER) WHERE created_ts IS NULL;")

        self._conn.commit()

    def add_message(
            self,
            origin_id: bytes,
            seqno: int,
            channel: str,
            nick: str,
            text: str,
            ts: Optional[float] = None,
            created_ts: Optional[int] = None,
    ) -> None:
        """
        Insert a message, ignoring if already present.

        Args:
            origin_id: sender node id
            seqno: per-origin message sequence number
            channel: channel / DM key
            nick: display nickname/callsign
            text: message body
            ts: local receive/insert time (float seconds since epoch)
            created_ts: sender-created timestamp (int unix seconds)
        """
        if ts is None:
            ts = time.time()
        if created_ts is None:
            created_ts = int(ts)

        insert_sql = """
        INSERT OR IGNORE INTO chat_messages
            (origin_id, seqno, channel, nick, text, ts, created_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?);
        """
        cur = self._conn.execute(
            insert_sql,
            (origin_id, int(seqno), channel, nick, text, float(ts), int(created_ts)),
        )
        self._conn.commit()

        # Fire hook only when a new row was inserted (not a deduped IGNORE).
        if cur.rowcount == 1 and self._on_message_stored is not None:
            try:
                self._on_message_stored(
                    {
                        "origin_id": origin_id,
                        "seqno": int(seqno),
                        "channel": channel,
                        "nick": nick,
                        "text": text,
                        "ts": float(ts),
                        "created_ts": int(created_ts),
                    }
                )
            except Exception:
                # Store must remain robust even if a hook misbehaves.
                pass

    def has_message(self, origin_id: bytes, seqno: int) -> bool:
        sql = """
        SELECT 1 FROM chat_messages
        WHERE origin_id = ? AND seqno = ?
        LIMIT 1;
        """
        cur = self._conn.execute(sql, (origin_id, int(seqno)))
        row = cur.fetchone()
        return row is not None

    def get_recent_messages(
            self,
            channel: str,
            limit: int = 100,
    ) -> List[Tuple[bytes, int, str, str, str, float]]:
        """
        Return messages for a channel ordered by created time.

        For combined timeline display, we order primarily by created_ts (sender time)
        and use row id as a stable tiebreaker. The returned last element is
        created_ts as float seconds (for UI formatting).
        """
        if limit <= 0:
            sql_all = """
            SELECT origin_id, seqno, channel, nick, text, created_ts
            FROM chat_messages
            WHERE channel = ?
            ORDER BY created_ts ASC, id ASC;
            """
            cur = self._conn.execute(sql_all, (channel,))
            rows = cur.fetchall()
            return [(r[0], int(r[1]), r[2], r[3], r[4], float(r[5])) for r in rows]

        sql = """
        SELECT id, origin_id, seqno, channel, nick, text, created_ts
        FROM chat_messages
        WHERE channel = ?
        ORDER BY id DESC
        LIMIT ?;
        """
        cur = self._conn.execute(sql, (channel, int(limit)))
        rows = cur.fetchall()

        # rows: (id, origin_id, seqno, channel, nick, text, created_ts)
        rows.sort(key=lambda r: (r[6], r[0]))
        return [(r[1], int(r[2]), r[3], r[4], r[5], float(r[6])) for r in rows]

    def get_messages_since(
            self,
            channel: str,
            since_ts: float,
            limit: int = 100,
    ) -> List[Tuple[bytes, int, str, str, str, float]]:
        """
        Return messages in a channel with created_ts > since_ts, ordered by created_ts.
        """
        sql = """
        SELECT origin_id, seqno, channel, nick, text, created_ts
        FROM chat_messages
        WHERE channel = ? AND created_ts > ?
        ORDER BY created_ts ASC
        LIMIT ?;
        """
        cur = self._conn.execute(sql, (channel, float(since_ts), int(limit)))
        rows = cur.fetchall()
        return [(r[0], int(r[1]), r[2], r[3], r[4], float(r[5])) for r in rows]

    def get_last_n_messages(
            self,
            channel: str,
            last_n: int,
    ) -> List[Tuple[bytes, int, str, str, str, float]]:
        """
        Return the last N messages for a channel, ordered by created_ts ascending.

        This is used for sync inventory windows.
        """
        if last_n <= 0:
            return []

        sql = """
        SELECT id, origin_id, seqno, channel, nick, text, created_ts
        FROM chat_messages
        WHERE channel = ?
        ORDER BY created_ts DESC, id DESC
        LIMIT ?;
        """
        cur = self._conn.execute(sql, (channel, int(last_n)))
        rows = cur.fetchall()
        rows.sort(key=lambda r: (r[6], r[0]))
        return [(r[1], int(r[2]), r[3], r[4], r[5], float(r[6])) for r in rows]

    def get_messages_for_origin_seq_range(
            self,
            channel: str,
            origin_id: bytes,
            start_seqno: int,
            end_seqno: int,
            limit: int = 200,
    ) -> List[Tuple[bytes, int, str, str, str, float]]:
        """
        Return messages for a specific origin_id within a seqno range (inclusive),
        scoped to a channel, ordered by seqno ascending.

        Used for targeted sync ("range" mode).
        """
        if start_seqno > end_seqno:
            start_seqno, end_seqno = end_seqno, start_seqno
        if limit <= 0:
            return []
        sql = """
        SELECT origin_id, seqno, channel, nick, text, created_ts
        FROM chat_messages
        WHERE channel = ? AND origin_id = ? AND seqno >= ? AND seqno <= ?
        ORDER BY seqno ASC
        LIMIT ?;
        """
        cur = self._conn.execute(
            sql, (channel, origin_id, int(start_seqno), int(end_seqno), int(limit))
        )
        rows = cur.fetchall()
        return [(r[0], int(r[1]), r[2], r[3], r[4], float(r[5])) for r in rows]

    def list_channels(self, limit: int = 50) -> List[str]:
        """
        Return distinct channel identifiers ordered by most recent activity
        by created time.
        """
        sql = """
        SELECT channel, MAX(created_ts) AS last_ts
        FROM chat_messages
        GROUP BY channel
        ORDER BY last_ts DESC
        LIMIT ?;
        """
        cur = self._conn.execute(sql, (int(limit),))
        rows = cur.fetchall()
        return [str(r[0]) for r in rows]

    def prune_keep_last_n_per_channel(self, keep_last_n: int) -> int:
        """
        Prune the database by keeping only the most recent `keep_last_n` messages
        per channel/DM.

        Returns:
            Number of rows deleted.
        """
        if keep_last_n < 1:
            raise ValueError("keep_last_n must be >= 1")

        channels = self.list_channels(limit=10_000)
        deleted_total = 0

        for chan in channels:
            delete_sql = """
            DELETE FROM chat_messages
            WHERE channel = ?
              AND id NOT IN (
                SELECT id FROM chat_messages
                WHERE channel = ?
                ORDER BY created_ts DESC, id DESC
                LIMIT ?
              );
            """
            cur = self._conn.execute(delete_sql, (chan, chan, int(keep_last_n)))
            deleted_total += int(cur.rowcount if cur.rowcount is not None else 0)

        self._conn.commit()
        return deleted_total

    def prune_older_than_seconds(self, older_than_seconds: int, channel: Optional[str] = None) -> int:
        """Prune messages older than a threshold (local-only).

        Deletes rows where created_ts < (now - older_than_seconds). If `channel` is provided,
        restricts deletion to that channel.

        Returns number of rows deleted.
        """
        secs = int(older_than_seconds)
        if secs <= 0:
            return 0
        cutoff = int(time.time()) - secs

        if channel is None:
            cur = self._conn.execute(
                "DELETE FROM chat_messages WHERE created_ts < ?",
                (cutoff,),
            )
        else:
            cur = self._conn.execute(
                "DELETE FROM chat_messages WHERE created_ts < ? AND channel = ?",
                (cutoff, str(channel)),
            )

        self._conn.commit()
        try:
            return int(cur.rowcount or 0)
        except Exception:
            return 0

    def get_db_stats(self) -> dict:
        """Return basic DB stats for diagnostics (local-only)."""
        try:
            cur = self._conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT channel), MIN(created_ts), MAX(created_ts) FROM chat_messages"
            )
            row = cur.fetchone()
        except sqlite3.Error:
            row = None

        if not row:
            return {"messages_total": 0, "channels": 0, "oldest_created_ts": None, "newest_created_ts": None}

        total, chans, oldest, newest = row
        return {
            "messages_total": int(total or 0),
            "channels": int(chans or 0),
            "oldest_created_ts": int(oldest) if oldest is not None else None,
            "newest_created_ts": int(newest) if newest is not None else None,
        }

    def close(self) -> None:
        self._conn.close()
