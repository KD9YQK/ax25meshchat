from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, List, Dict, Any
import json
import struct
import time

# Protocol version:
# - v1: [ver][type][chan_len][nick_len][chan][nick][text]
# - v2: [ver][type][chan_len][nick_len][created_ts_u32][chan][nick][text]
CHAT_VERSION = 2

CHAT_TYPE_MESSAGE = 1
CHAT_TYPE_SYNC_REQUEST = 5
CHAT_TYPE_SYNC_RESPONSE = 6


@dataclass
class ChatMessage:
    msg_type: int
    channel: str
    nick: str
    text: str
    # Unix UTC seconds when the message was created (sender-side).
    created_ts: int


def encode_chat_message(msg: ChatMessage) -> bytes:
    """
    Encode a chat message.

    v1: [ver][type][chan_len][nick_len][chan][nick][text]
    v2: [ver][type][chan_len][nick_len][created_ts_u32][chan][nick][text]
    """
    channel_bytes = msg.channel.encode("utf-8")
    nick_bytes = msg.nick.encode("utf-8")
    text_bytes = msg.text.encode("utf-8")

    chan_len = len(channel_bytes)
    nick_len = len(nick_bytes)

    if chan_len > 255:
        raise ValueError("channel name too long")
    if nick_len > 255:
        raise ValueError("nick too long")

    header = bytearray(4)
    header[0] = CHAT_VERSION
    header[1] = msg.msg_type
    header[2] = chan_len
    header[3] = nick_len

    created_ts = int(msg.created_ts)
    if created_ts < 0 or created_ts > 0xFFFFFFFF:
        raise ValueError("created_ts out of range for uint32")
    ts_bytes = struct.pack(">I", created_ts)

    return bytes(header) + ts_bytes + channel_bytes + nick_bytes + text_bytes


def decode_chat_message(data: bytes) -> Optional[ChatMessage]:
    if len(data) < 4:
        return None

    version = data[0]
    msg_type = data[1]
    chan_len = data[2]
    nick_len = data[3]

    # Backward compatibility: v1 had no created_ts.
    if version == 1:
        header_len = 4
        needed = header_len + chan_len + nick_len
        if len(data) < needed:
            return None

        channel_bytes = data[header_len: header_len + chan_len]
        nick_bytes = data[header_len + chan_len: header_len + chan_len + nick_len]
        text_bytes = data[header_len + chan_len + nick_len:]

        return ChatMessage(
            msg_type=msg_type,
            channel=channel_bytes.decode("utf-8", errors="replace"),
            nick=nick_bytes.decode("utf-8", errors="replace"),
            text=text_bytes.decode("utf-8", errors="replace"),
            created_ts=int(time.time()),
        )

    if version != CHAT_VERSION:
        return None

    header_len = 4
    if len(data) < header_len + 4:
        return None
    created_ts = struct.unpack(">I", data[header_len: header_len + 4])[0]
    header_len += 4

    needed = header_len + chan_len + nick_len
    if len(data) < needed:
        return None

    channel_bytes = data[header_len: header_len + chan_len]
    nick_bytes = data[header_len + chan_len: header_len + chan_len + nick_len]
    text_bytes = data[header_len + chan_len + nick_len:]

    return ChatMessage(
        msg_type=msg_type,
        channel=channel_bytes.decode("utf-8", errors="replace"),
        nick=nick_bytes.decode("utf-8", errors="replace"),
        text=text_bytes.decode("utf-8", errors="replace"),
        created_ts=int(created_ts),
    )


# --------------------- SYNC helpers ---------------------

@dataclass(frozen=True)
class SyncRequest:
    # mode="since_ts" uses {"since_ts": float} (interpreted as created_ts seconds)
    # mode="seqno" uses {"mode":"seqno","last_n":int,"inv":{origin_hex:int}}
    mode: str
    since_ts: Optional[float]
    last_n: int
    inv: Dict[str, int]


def encode_sync_request(channel: str, nick: str, since_ts: float) -> bytes:
    """
    Backwards-compatible SYNC_REQUEST v1: text = JSON {"since_ts": float}

    Note: since_ts is interpreted as "created_ts" (unix seconds).
    """
    payload = {"since_ts": since_ts}
    msg = ChatMessage(
        msg_type=CHAT_TYPE_SYNC_REQUEST,
        channel=channel,
        nick=nick,
        text=json.dumps(payload),
        created_ts=int(time.time()),
    )
    return encode_chat_message(msg)


def encode_sync_request_seqno(
    channel: str,
    nick: str,
    last_n: int,
    inv: Dict[str, int],
) -> bytes:
    """
    SYNC_REQUEST v2 (seqno inventory):
      text = JSON {"mode":"seqno","last_n":int,"inv":{origin_id_hex:int}}
    """
    payload: Dict[str, Any] = {
        "mode": "seqno",
        "last_n": int(last_n),
        "inv": inv,
    }
    msg = ChatMessage(
        msg_type=CHAT_TYPE_SYNC_REQUEST,
        channel=channel,
        nick=nick,
        text=json.dumps(payload),
        created_ts=int(time.time()),
    )
    return encode_chat_message(msg)


def parse_sync_request_any(msg: ChatMessage) -> Optional[SyncRequest]:
    """
    Parse either v1 {"since_ts": ...} or v2 {"mode":"seqno",...}.
    """
    try:
        obj = json.loads(msg.text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    mode = obj.get("mode")
    if mode == "seqno":
        last_n = obj.get("last_n")
        inv = obj.get("inv")
        if not isinstance(last_n, int):
            return None
        if not isinstance(inv, dict):
            return None

        inv_clean: Dict[str, int] = {}
        for k, v in inv.items():
            if isinstance(k, str) and isinstance(v, int):
                inv_clean[k] = v

        return SyncRequest(mode="seqno", since_ts=None, last_n=int(last_n), inv=inv_clean)

    # v1
    since_ts = obj.get("since_ts")
    if not isinstance(since_ts, (float, int)):
        return None
    return SyncRequest(mode="since_ts", since_ts=float(since_ts), last_n=0, inv={})


# Backward-compatible name for older code:
def parse_sync_request(msg: ChatMessage) -> Optional[float]:
    """
    Old API expected by chat_client.py: returns since_ts or None.
    """
    req = parse_sync_request_any(msg)
    if req is None:
        return None
    if req.mode != "since_ts":
        return None
    return req.since_ts


def encode_sync_response(
    channel: str,
    nick: str,
    records: List[Dict[str, Any]],
) -> bytes:
    """
    SYNC_RESPONSE: text = JSON list of records:
      {"origin_id_hex": str, "seqno": int, "nick": str, "text": str, "ts": int}

    Note: "ts" is the created timestamp (unix seconds).
    """
    msg = ChatMessage(
        msg_type=CHAT_TYPE_SYNC_RESPONSE,
        channel=channel,
        nick=nick,
        text=json.dumps(records),
        created_ts=int(time.time()),
    )
    return encode_chat_message(msg)


def parse_sync_response(msg: ChatMessage) -> Optional[List[Dict[str, Any]]]:
    try:
        obj = json.loads(msg.text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, list):
        return None
    return obj
