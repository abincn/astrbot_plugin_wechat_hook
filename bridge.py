"""Compatibility exports for the former combined bridge module.

New code should import each responsibility from identity, events or inbox.
"""
from .events import Inbound, normalize_event
from .identity import group_session, split_session
from .inbox import DurableInbox

__all__ = [
    "DurableInbox",
    "Inbound",
    "group_session",
    "normalize_event",
    "split_session",
]
