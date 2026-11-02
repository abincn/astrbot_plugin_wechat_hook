"""Deterministic per-source draft identity, independent of AstrBot imports."""
from __future__ import annotations
import hashlib
from .identity import conversation_session


def draft_intent(owner: str, room: str, source_event_id: str, ordinal: int) -> str:
    conversation_session(owner, room)
    if not source_event_id or len(source_event_id) > 220:
        raise ValueError("Missing or oversized source message ID")
    if type(ordinal) is not int or ordinal < 0 or ordinal > 10000:
        raise ValueError("Invalid reply ordinal")
    material="\x1f".join((owner,room,source_event_id,str(ordinal)))
    return "draft-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:48]


def source_draft_identity(source, adapter, session_id: str, owner: str, room: str) -> tuple[str, str]:
    """Bind a reply to the current, same-adapter/same-conversation message.

    The caller must first verify source is its own HookMessageEvent instance;
    this function is a pure, easily testable guard for all remaining identities.
    A missing or mismatched source is explicitly unlinked.
    """
    if source is None or getattr(source, "adapter", None) is not adapter:
        return "", ""
    session = getattr(source, "session", None)
    message = getattr(source, "message_obj", None)
    destination = getattr(message, "group_id", None) or getattr(getattr(message, "sender", None), "user_id", None)
    if (getattr(session, "session_id", None) != session_id
            or getattr(message, "self_id", None) != owner
            or destination != room
            or conversation_session(owner, room) != session_id
            or getattr(message, "type", None) != getattr(session, "message_type", None)):
        return "", ""
    source_id = str(getattr(message, "message_id", "") or "")
    if not source_id or len(source_id) > 220:
        return "", ""
    index = source.next_draft_index()
    return source_id, draft_intent(owner, room, source_id, index)
