"""Resolve display names through existing read-only Hook APIs, with bounded caching."""
import asyncio
from collections import OrderedDict
from dataclasses import replace
import time

import httpx
from astrbot.api import logger


def display_name(value) -> str:
    return "".join(c for c in value if c.isprintable()).strip()[:160] if isinstance(value, str) else ""


class ContactNames:
    def __init__(self, request):
        self.request = request
        self.cache = OrderedDict()

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
                    _, response = await self.request("POST", "/BatchGetContactBriefInfo", data={"wxidlist": sorted(missing)[:200]})
                    rows = response.get("contacts") if isinstance(response, dict) else None
                    for row in rows[:200] if isinstance(rows, list) else []:
                        if not isinstance(row, dict) or row.get("UserName") not in missing:
                            continue
                        name = display_name(row.get("NickName"))
                        if name:
                            self.put(("user", row["UserName"]), name)
                for room in sorted(groups):
                    self.put(("group", room), {}, 60)
                    _, response = await self.request("POST", "/GetContact", data={"wxid": room})
                    members = response.get("members") if isinstance(response, dict) else None
                    names = {}
                    for member in members[:4096] if isinstance(members, list) else []:
                        if isinstance(member, dict) and isinstance(member.get("wxid"), str):
                            name = display_name(member.get("nickname"))
                            if name:
                                names[member["wxid"]] = name
                    self.put(("group", room), names, 600 if names else 60)
        except (TimeoutError, RuntimeError, httpx.HTTPError, ValueError, TypeError) as exc:
            logger.debug(f"Hook display-name lookup unavailable ({type(exc).__name__}); keeping known names/IDs")
        result = []
        for msg in messages:
            group_name = (self.get(("group", msg.room)) or {}).get(msg.sender, "") if msg.is_group else ""
            known = msg.sender_name if msg.sender_name != msg.sender else ""
            name = group_name or known or self.get(("user", msg.sender)) or msg.sender
            result.append(replace(msg, sender_name=name))
        return result
