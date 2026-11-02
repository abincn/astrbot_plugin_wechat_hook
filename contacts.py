"""Resolve display names through existing read-only Hook APIs, with bounded caching."""
import asyncio
from collections import OrderedDict
from dataclasses import replace
import time

import httpx
from astrbot.api import logger
from .contact_db import ContactDatabase


def display_name(value) -> str:
    return "".join(c for c in value if c.isprintable()).strip()[:160] if isinstance(value, str) else ""


def field(row, *names):
    if not isinstance(row, dict):
        return ""
    normalized = {str(key).replace("_", "").lower(): value for key, value in row.items()}
    for name in names:
        value = normalized.get(name.replace("_", "").lower())
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def rows_from(response, limit=200):
    if isinstance(response, list):
        return [row for row in response[:limit] if isinstance(row, dict)]
    if isinstance(response, dict):
        for key in ("contacts", "data", "contact", "result"):
            value = response.get(key)
            if isinstance(value, (list, dict)):
                # Only unwrap a bounded number of known envelope levels.
                if isinstance(value, list):
                    return [row for row in value[:limit] if isinstance(row, dict)]
                return [value]
        return [response]
    return []


class ContactNames:
    def __init__(self, request):
        self.request = request
        self.cache = OrderedDict()
        self.database = ContactDatabase(self.lookup)

    def get(self, key):
        entry = self.cache.get(key)
        if entry is None or entry[0] <= time.monotonic():
            return None
        self.cache.move_to_end(key)
        return entry[1]

    def put(self, key, value, ttl=600):
        self.cache[key] = (time.monotonic() + ttl, value)
        self.cache.move_to_end(key)
        while len(self.cache) > 512:
            self.cache.popitem(last=False)

    async def lookup(self, route, data):
        try:
            _, response = await self.request("POST", route, data=data)
            return response
        except (RuntimeError, httpx.HTTPError, ValueError, TypeError) as exc:
            status = getattr(exc, "status_code", None)
            logger.debug(f"Hook contact lookup unavailable: route={route}, status={status}, error={type(exc).__name__}")
            return None

    async def resolve(self, messages):
        missing = {msg.sender for msg in messages if msg.sender_name == msg.sender
                   and msg.sender != "wechat_system" and self.get(("user", msg.sender)) is None}
        groups = {msg.room for msg in messages if msg.is_group and msg.sender_name == msg.sender
                  and self.get(("group", msg.room)) is None}
        try:
            # Name lookup must never hold up a message batch indefinitely.
            async with asyncio.timeout(3):
                if missing:
                    for uid in missing:
                        self.put(("user", uid), "", 60)
                    response = await self.lookup("/BatchGetContactBriefInfo", {"wxidlist": sorted(missing)[:200]})
                    for row in rows_from(response):
                        uid = field(row, "UserName", "wxid")
                        if uid not in missing:
                            continue
                        name = display_name(field(row, "NickName", "display_name"))
                        if name and name != uid:
                            self.put(("user", uid), name)
                    unresolved = sorted(uid for uid in missing if not self.get(("user", uid)))
                    for uid, name in (await self.database.resolve(unresolved)).items():
                        name = display_name(name)
                        if name and name != uid:
                            self.put(("user", uid), name)
                    for uid in sorted(missing):
                        if self.get(("user", uid)):
                            continue
                        # Batch SQL selects optional columns and can fail while SELECT * still works.
                        response = await self.lookup("/GetContact", {"wxid": uid})
                        contact_rows = rows_from(response, 1)
                        row = contact_rows[0] if contact_rows else {}
                        returned_id = field(row, "UserName", "wxid")
                        name = display_name(field(row, "NickName", "display_name"))
                        if returned_id == uid and name and name != uid:
                            self.put(("user", uid), name)
                        else:
                            keys = ",".join(str(key)[:40] for key in list(row)[:20])
                            logger.warning(f"Hook nickname not resolved: sender={uid}, contact_fields=[{keys}]; showing explicit missing-name label")
                for room in sorted(groups):
                    self.put(("group", room), {}, 60)
                    response = await self.lookup("/GetContact", {"wxid": room})
                    group_rows = rows_from(response, 1)
                    group_row = group_rows[0] if group_rows else {}
                    members = group_row.get("members")
                    names = {}
                    for member in members[:4096] if isinstance(members, list) else []:
                        uid = field(member, "wxid", "UserName")
                        name = display_name(field(member, "nickname", "display_name"))
                        if uid and name and name != uid:
                            names[uid] = name
                    self.put(("group", room), names, 600 if names else 60)
        except (TimeoutError, RuntimeError, httpx.HTTPError, ValueError, TypeError) as exc:
            logger.warning(f"Hook display-name lookup incomplete ({type(exc).__name__}); keeping known names and marking missing nicknames")
        result = []
        for msg in messages:
            group_name = (self.get(("group", msg.room)) or {}).get(msg.sender, "") if msg.is_group else ""
            known = msg.sender_name if msg.sender_name != msg.sender else ""
            name = group_name or known or self.get(("user", msg.sender)) or ("微信系统" if msg.sender == "wechat_system" else "昵称未取得")
            result.append(replace(msg, sender_name=name))
        return result
