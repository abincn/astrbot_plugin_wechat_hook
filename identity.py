"""Stable account/conversation identities; existing group session IDs stay intact."""
from __future__ import annotations

import hashlib
import re

def stable_hash(parts: list[str]) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def group_session(owner: str, room: str) -> str:
    if not owner or ":" in owner or "|" in owner or not room.endswith("@chatroom") or "|" in room or ":" in room:
        raise ValueError("Invalid WeChat group identity")
    return owner + "|" + room


def direct_session(owner: str, peer: str) -> str:
    if (not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.@-]{1,256}", value)
                for value in (owner, peer)) or peer.endswith("@chatroom") or owner == peer):
        raise ValueError("Invalid WeChat private identity")
    return owner + "|dm|" + peer


def conversation_session(owner: str, destination: str) -> str:
    return group_session(owner, destination) if destination.endswith("@chatroom") else direct_session(owner, destination)


def split_session(session: str) -> tuple[str, str]:
    try:
        owner, room = session.split("|", 1)
        if room.startswith("dm|"):
            peer = room[3:]
            direct_session(owner, peer)
            return owner, peer
        group_session(owner, room)
        return owner, room
    except (ValueError, AttributeError) as exc:
        raise ValueError("Not an allowed WeChat conversation session") from exc


