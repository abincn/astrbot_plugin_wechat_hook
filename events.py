"""Normalize raw Hook payloads into validated inbound group messages."""
from __future__ import annotations

import time
from dataclasses import dataclass

from .identity import group_session, stable_hash

@dataclass(frozen=True)
class Inbound:
    event_id: str
    sequence: int
    owner: str
    room: str
    sender: str
    sender_name: str
    content: str
    kind: int
    at_me: bool | None
    created_at: int
    raw: dict
    media_kind: str = ""
    media_msgsvrid: str = ""
    media_source_talker: str = ""
    media_path: str = ""
    quote_text: str = ""

    @property
    def session_id(self) -> str:
        return group_session(self.owner, self.room)


def normalize_event(event: dict, self_wxid: str, allowed_rooms: set[str]) -> Inbound | None:
    """Accept only explicitly incoming group events; never infer a native @ from text."""
    if not isinstance(event, dict):
        raise ValueError("Hook event must be a JSON object")
    if event.get("event_type", "message") != "message":
        return None
    if event.get("direction") != "incoming" or event.get("is_group") is not True:
        return None
    room = str(event.get("room_id") or event.get("conversation_id") or "")
    if not room.endswith("@chatroom") or room not in allowed_rooms:
        return None
    owner = str(event.get("self_id") or self_wxid or "")
    if not owner or (self_wxid and owner != self_wxid):
        raise ValueError("Hook self_id missing or differs from configured account")
    sender = str(event.get("sender_id") or "")
    if not sender or sender.endswith("@chatroom") or sender == owner:
        return None
    group_session(owner, room)
    sequence = event.get("sequence", event.get("seq", 0))
    if type(sequence) not in (int, str) or not str(sequence).isdigit():
        raise ValueError("Hook sequence is not a nonnegative integer")
    sequence = int(sequence)
    if sequence < 0:
        raise ValueError("Hook sequence is negative")
    try:
        kind = int(event.get("type") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("Hook message type is invalid") from exc
    if kind not in (1, 3, 34, 43, 47, 49):
        return None

    text = str(event.get("content") or event.get("raw_content") or "")[:30000]
    at_me = event.get("is_at_me", event.get("at_me"))
    if isinstance(at_me, int) and at_me in (0, 1):
        at_me = bool(at_me)
    if not isinstance(at_me, bool):
        at_me = None

    source_meta = event.get("source_meta") if isinstance(event.get("source_meta"), dict) else {}
    message_kind = str(event.get("message_kind") or source_meta.get("message_kind") or "")
    quote = event.get("quote") if isinstance(event.get("quote"), dict) else {}
    media_kind = "image" if kind == 3 else "video" if kind == 43 else ""
    media_msgsvrid = str(event.get("msgsvrid") or "")
    media_source_talker = sender
    if kind == 49 and message_kind == "file_card":
        media_kind = "file"
    elif kind == 49 and message_kind == "quote":
        qmedia = quote.get("media") if isinstance(quote.get("media"), dict) else {}
        if qmedia.get("kind") in ("image", "video") and str(quote.get("msgsvrid") or "").isdecimal():
            media_kind = str(qmedia["kind"])
            media_msgsvrid = str(quote["msgsvrid"])
            media_source_talker = str(quote.get("sender_id") or sender)

    event_id = str(event.get("event_id") or "")
    if not event_id:
        stable = media_msgsvrid if media_msgsvrid.isdecimal() else str(sequence) + ":" + text
        event_id = "hook:" + stable_hash([owner, room, sender, stable, str(kind)])
    if len(event_id) > 220:
        raise ValueError("Hook event ID exceeds limit")
    stamp_raw = event.get("message_time") or event.get("created_at") or time.time()
    try:
        stamp = int(float(stamp_raw))
    except (TypeError, ValueError, OverflowError):
        stamp = int(time.time())
    quote_text = str(quote.get("content") or quote.get("reply_text") or "")[:4500]
    sender_name = str(event.get("sender_nick") or event.get("sender_name") or sender)[:160]
    return Inbound(event_id, sequence, owner, room, sender, sender_name, text, kind,
                   at_me, stamp, event, media_kind, media_msgsvrid,
                   media_source_talker, "", quote_text)


