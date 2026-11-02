"""Stable WeChat account/group identity helpers."""
from __future__ import annotations

import hashlib

def stable_hash(parts: list[str]) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def group_session(owner: str, room: str) -> str:
    if not owner or ":" in owner or "|" in owner or not room.endswith("@chatroom") or "|" in room or ":" in room:
        raise ValueError("Invalid WeChat group identity")
    return owner + "|" + room


def split_session(session: str) -> tuple[str, str]:
    try:
        owner, room = session.split("|", 1)
        group_session(owner, room)
        return owner, room
    except (ValueError, AttributeError) as exc:
        raise ValueError("Not an allowed WeChat group session") from exc


