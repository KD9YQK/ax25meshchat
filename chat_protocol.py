# chat_protocol.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, List, Dict, Any
import json

CHAT_VERSION = 1

CHAT_TYPE_MESSAGE = 1
CHAT_TYPE_SYNC_REQUEST = 5
CHAT_TYPE_SYNC_RESPONSE = 6


@dataclass
class ChatMessage:
    msg_type: int
    channel: str
    nick: str
    text: str


def encode_chat_message(msg: ChatMessage) -> bytes:
    """
    Basic chat message encoder: [ver][type][chan_len][nick_len][chan][nick][text]
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

    payload = bytes(header) + channel_bytes + nick_bytes + text_bytes
    return payload


def decode_chat_message(data: bytes) -> Optional[ChatMessage]:
    if len(data) < 4:
        return None

    version = data[0]
    msg_type = data[1]
    chan_len = data[2]
    nick_len = data[3]

    if version != CHAT_VERSION:
        return None

    header_len = 4
    needed = header_len + chan_len + nick_len
    if len(data) < needed:
        return None

    channel_bytes = data[header_len: header_len + chan_len]
    nick_bytes = data[header_len + chan_len: header_len + chan_len + nick_len]
    text_bytes = data[header_len + chan_len + nick_len:]

    channel = channel_bytes.decode("utf-8", errors="replace")
    nick = nick_bytes.decode("utf-8", errors="replace")
    text = text_bytes.decode("utf-8", errors="replace")

    return ChatMessage(
        msg_type=msg_type,
        channel=channel,
        nick=nick,
        text=text,
    )


# --------------------- SYNC helpers ---------------------


def encode_sync_request(channel: str, nick: str, since_ts: float) -> bytes:
    """
    SYNC_REQUEST: text = JSON {"since_ts": float}
    """
    payload = {
        "since_ts": since_ts,
    }
    msg = ChatMessage(
        msg_type=CHAT_TYPE_SYNC_REQUEST,
        channel=channel,
        nick=nick,
        text=json.dumps(payload),
    )
    return encode_chat_message(msg)


def parse_sync_request(msg: ChatMessage) -> Optional[float]:
    """
    Returns since_ts or None on error.
    """
    try:
        obj = json.loads(msg.text)
    except json.JSONDecodeError:
        return None
    if "since_ts" not in obj:
        return None
    value = obj["since_ts"]
    if not isinstance(value, (float, int)):
        return None
    return float(value)


def encode_sync_response(
        channel: str,
        nick: str,
        records: List[Dict[str, Any]],
) -> bytes:
    """
    SYNC_RESPONSE: text = JSON list of records:
      {"origin_id_hex": str, "seqno": int, "nick": str, "text": str, "ts": float}
    """
    msg = ChatMessage(
        msg_type=CHAT_TYPE_SYNC_RESPONSE,
        channel=channel,
        nick=nick,
        text=json.dumps(records),
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
