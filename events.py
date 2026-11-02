"""Normalize explicitly incoming Hook group and private messages."""
from __future__ import annotations

import time
from dataclasses import dataclass

from .identity import conversation_session, stable_hash

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
    media_error: str = ""

    @property
    def session_id(self) -> str:
        return conversation_session(self.owner, self.room)

    @property
    def is_group(self) -> bool:
        return self.raw.get("is_group") is True

    @property
    def quote(self) -> dict | None:
        metadata = self.raw.get("source_meta")
        metadata = metadata if isinstance(metadata, dict) else {}
        kind = self.raw.get("message_kind") or metadata.get("message_kind")
        quote = self.raw.get("quote")
        return quote if self.kind == 49 and kind == "quote" and isinstance(quote, dict) else None


def normalize_event(event: dict, self_wxid: str) -> Inbound | None:
    """Require explicit direction and conversation identity; don't infer @ from text."""
    if not isinstance(event, dict):
        raise ValueError("Hook event must be a JSON object")
    if event.get("event_type", "message") not in ("message", "file_transfer"):
        return None
    if event.get("direction") != "incoming" or type(event.get("is_group")) is not bool:
        return None
    is_group = event["is_group"]
    room = str((event.get("room_id") or event.get("conversation_id")) if is_group
               else event.get("conversation_id") or "")
    if is_group and not room.endswith("@chatroom"):
        return None
    owner = str(event.get("self_id") or self_wxid or "")
    if not owner or (self_wxid and owner != self_wxid):
        raise ValueError("Hook self_id missing or differs from configured account")
    sender = str(event.get("sender_id") or "")
    if event.get("type") in (10000, 10002, "10000", "10002") and is_group and (not sender or sender == room):
        sender = "wechat_system"
    if not sender or sender.endswith("@chatroom") or sender == owner:
        return None
    if not is_group and (room != sender or event.get("room_id")):
        return None
    conversation_session(owner, room)
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
    if not 0 < kind <= 0xFFFFFFFF:
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
    media_kind = {3: "image", 43: "video", 34: "voice", 47: "emoji"}.get(kind, "")
    media_msgsvrid = str(event.get("msgsvrid") or "")
    # Hook's source_talker locates a conversation table, not a group member.
    media_source_talker = room
    if kind == 49 and message_kind == "file_card":
        media_kind = "file"
    elif kind == 49 and message_kind == "quote":
        qmedia = quote.get("media") if isinstance(quote.get("media"), dict) else {}
        if qmedia.get("kind") in ("image", "video") and str(quote.get("msgsvrid") or "").isdecimal():
            media_kind = str(qmedia["kind"])
            media_msgsvrid = str(quote["msgsvrid"])
            quoted_conversation = str(quote.get("conversation_id") or "")
            if quoted_conversation and quoted_conversation != owner and quoted_conversation.endswith("@chatroom") == is_group:
                try:
                    conversation_session(owner, quoted_conversation)
                    media_source_talker = quoted_conversation
                except ValueError:
                    pass

    event_id = str(event.get("event_id") or "")
    if not event_id:
        outer_id = str(event.get("msgsvrid") or "")
        stable = outer_id if outer_id.isdecimal() else str(sequence) + ":" + text
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


