# chat_store.py

from __future__ import annotations

import sqlite3
import time
from typing import List, Tuple, Optional


class ChatStore:
    """
    Persistent chat log using SQLite.

    - One DB file per node (configurable via path).
    - Deduplicates messages by (origin_id, seqno).
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

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
            UNIQUE(origin_id, seqno)
        );
        """
        self._conn.execute(create_sql)
        self._conn.commit()

    def add_message(
            self,
            origin_id: bytes,
            seqno: int,
            channel: str,
            nick: str,
            text: str,
            ts: Optional[float] = None,
    ) -> None:
        """
        Insert a message, ignoring if already present.
        """
        if ts is None:
            ts = time.time()

        insert_sql = """
        INSERT OR IGNORE INTO chat_messages
            (origin_id, seqno, channel, nick, text, ts)
        VALUES (?, ?, ?, ?, ?, ?);
        """
        self._conn.execute(
            insert_sql,
            (origin_id, seqno, channel, nick, text, ts),
        )
        self._conn.commit()

    def has_message(self, origin_id: bytes, seqno: int) -> bool:
        sql = """
        SELECT 1 FROM chat_messages
        WHERE origin_id = ? AND seqno = ?
        LIMIT 1;
        """
        cur = self._conn.execute(sql, (origin_id, seqno))
        row = cur.fetchone()
        return row is not None

    def get_recent_messages(
            self,
            channel: str,
            limit: int = 100,
    ) -> List[Tuple[bytes, int, str, str, str, float]]:
        """
        Return the most recent messages in a channel, ordered oldest → newest.

        Note:
            This returns the *last* `limit` messages (newest messages), not the first `limit`
            rows in the database. Internally we select newest-first with a LIMIT, then
            reorder ascending for display.
        """
        sql = """
        SELECT origin_id, seqno, channel, nick, text, ts
        FROM (
            SELECT origin_id, seqno, channel, nick, text, ts, id
            FROM chat_messages
            WHERE channel = ?
            ORDER BY ts DESC, id DESC
            LIMIT ?
        )
        ORDER BY ts ASC, id ASC;
        """
        cur = self._conn.execute(sql, (channel, limit))
        rows = cur.fetchall()
        return rows

    def get_messages_since(
            self,
            channel: str,
            since_ts: float,
            limit: int = 100,
    ) -> List[Tuple[bytes, int, str, str, str, float]]:
        """
        Return messages in a channel with ts > since_ts, ordered by ts.
        """
        sql = """
        SELECT origin_id, seqno, channel, nick, text, ts
        FROM chat_messages
        WHERE channel = ? AND ts > ?
        ORDER BY ts ASC
        LIMIT ?;
        """
        cur = self._conn.execute(sql, (channel, since_ts, limit))
        rows = cur.fetchall()
        return rows

    def list_channels(self, limit: int = 50) -> List[str]:
        """
        Return distinct channel identifiers ordered by most recent activity.
        This includes normal channels (e.g. '#general') and DM channel keys
        (whatever naming convention the client uses).
        """
        sql = """
        SELECT channel, MAX(ts) AS last_ts
        FROM chat_messages
        GROUP BY channel
        ORDER BY last_ts DESC
        LIMIT ?;
        """
        cur = self._conn.execute(sql, (limit,))
        rows = cur.fetchall()
        return [str(r[0]) for r in rows]

    def prune_keep_last_n_per_channel(self, keep_last_n: int) -> int:
        """
        Prune the database by keeping only the most recent `keep_last_n` messages
        per channel/DM (as identified by the `channel` column).

        Returns:
            Number of rows deleted.
        """
        if keep_last_n < 1:
            raise ValueError("keep_last_n must be >= 1")

        # Determine channels first (including DMs, which are stored as channels)
        channels = self.list_channels(limit=10_000)
        deleted_total = 0

        for chan in channels:
            # Delete all rows for this channel whose id is not in the newest keep_last_n.
            # Use a subquery selecting newest rows by ts (and id as a tiebreaker).
            delete_sql = """
            DELETE FROM chat_messages
            WHERE channel = ?
              AND id NOT IN (
                SELECT id FROM chat_messages
                WHERE channel = ?
                ORDER BY ts DESC, id DESC
                LIMIT ?
              );
            """
            cur = self._conn.execute(delete_sql, (chan, chan, keep_last_n))
            deleted_total += int(cur.rowcount if cur.rowcount is not None else 0)

        self._conn.commit()
        return deleted_total

    def close(self) -> None:
        self._conn.close()
