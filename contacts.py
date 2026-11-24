"""Bounded background nickname lookup; native DB activity never blocks message dispatch."""
import asyncio
from collections import OrderedDict
from dataclasses import replace
import json
import sqlite3
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
                return [row for row in value[:limit] if isinstance(row, dict)] if isinstance(value, list) else [value]
        return [response]
    return []


class ContactDeferred(RuntimeError):
    """The native query is waiting for WeChat's own contact database activity."""


class ContactNames:
    FOREGROUND_WAIT = 0.15
    RETRY_DELAY = 5
    MAX_ATTEMPTS = 3
    PREFETCH_CONTACTS = True
    # A refresh deadline isn't an instruction to discard a verified nickname.
    STALE_GRACE = 30 * 86400

    def __init__(self, request, *, cache_path=None, owner=""):
        self.request = request
        self.cache = OrderedDict()
        self.database = ContactDatabase(self.lookup)
        self.native_contacts = False
        self._pending = OrderedDict()
        self._inflight = set()
        self._misses = OrderedDict()
        self._task = None
        self._closed = False
        self._store = None
        self._dirty = False
        self._owner = owner
        if cache_path is not None and owner:
            try:
                self._store = sqlite3.connect(str(cache_path))
                self._store.execute("CREATE TABLE IF NOT EXISTS names (owner TEXT,kind TEXT,id TEXT,value TEXT,expires REAL,PRIMARY KEY(owner,kind,id))")
                self._store.execute("DELETE FROM names WHERE expires<=?", (time.time() - self.STALE_GRACE,))
                for kind, uid, value, expires in self._store.execute(
                    "SELECT kind,id,value,expires FROM names WHERE owner=? AND expires>? ORDER BY expires DESC LIMIT 512", (owner, time.time())):
                    name = json.loads(value)
                    if kind == "user":
                        name = display_name(name)
                    elif kind == "group" and isinstance(name, dict):
                        name = {key: display_name(val) for key, val in name.items() if isinstance(key, str)}
                    else:
                        continue
                    if name:
                        self.cache[(kind, uid)] = (time.monotonic() + max(0, expires - time.time()), name)
                self._store.commit()
            except (sqlite3.Error, ValueError, TypeError):
                if self._store is not None:
                    self._store.close()
                self._store = None
                logger.warning("Hook nickname cache unavailable; continuing with in-memory lookup")
        logger.info(f"Hook nickname background resolver ready: revision=20261119-01; cached={len(self.cache)}")

    def set_runtime(self, version):
        if isinstance(version, str):
            previous = self.native_contacts
            self.native_contacts = version.startswith("4.1.15.")
            if self.native_contacts and not previous and self.PREFETCH_CONTACTS:
                self._queue(("snapshot", "contacts"))

    def get(self, key, *, allow_stale=False):
        entry = self.cache.get(key)
        grace = self.STALE_GRACE if allow_stale else 0
        if entry is None or entry[0] + grace <= time.monotonic():
            if self._store is None or key[0] not in ("user", "group"):
                return None
            try:
                now = time.time()
                row = self._store.execute("SELECT value,expires FROM names WHERE owner=? AND kind=? AND id=? AND expires>?", (self._owner, key[0], key[1], now - grace)).fetchone()
                if row is None:
                    return None
                value = json.loads(row[0])
                if key[0] == "user":
                    value = display_name(value)
                elif not isinstance(value, dict):
                    return None
                self.cache[key] = (time.monotonic() + row[1] - now, value)
                self.cache.move_to_end(key)
                while len(self.cache) > 512:
                    self.cache.popitem(last=False)
                return value
            except (sqlite3.Error, ValueError, TypeError):
                return None
        self.cache.move_to_end(key)
        return entry[1]

    def put(self, key, value, ttl=86400):
        if not value:
            self._misses[key] = time.monotonic() + ttl
            self._misses.move_to_end(key)
            while len(self._misses) > 512:
                self._misses.popitem(last=False)
            return
        self._misses.pop(key, None)
        now = time.monotonic()
        previous = self.cache.get(key)
        if previous is not None and previous[1] == value and previous[0] >= now + ttl / 2:
            self.cache.move_to_end(key)
            return
        self.cache[key] = (now + ttl, value)
        self.cache.move_to_end(key)
        while len(self.cache) > 512:
            self.cache.popitem(last=False)
        if self._store is not None:
            try:
                self._store.execute("INSERT OR REPLACE INTO names VALUES(?,?,?,?,?)", (self._owner, key[0], key[1], json.dumps(value, ensure_ascii=False), time.time() + ttl))
                self._dirty = True
            except sqlite3.Error:
                self._store.rollback()
                logger.warning("Hook nickname cache write failed; keeping resolved names in memory")

    def _flush_cache(self):
        if self._store is not None and self._dirty:
            try:
                self._store.execute("DELETE FROM names WHERE owner=? AND rowid NOT IN (SELECT rowid FROM names WHERE owner=? ORDER BY expires DESC LIMIT 4096)", (self._owner, self._owner))
                self._store.commit()
            except sqlite3.Error:
                self._store.rollback()
                logger.warning("Hook nickname cache commit failed; keeping resolved names in memory")
            self._dirty = False

    async def lookup(self, route, data):
        try:
            _, response = await self.request("POST", route, data=data)
            return response
        except (RuntimeError, httpx.HTTPError, ValueError, TypeError) as exc:
            status = getattr(exc, "status_code", None)
            code = getattr(exc, "error_code", "")
            if status in (409, 504) and code in ("contact_query_timeout", "contact_query_busy"):
                raise ContactDeferred(code) from exc
            logger.debug(f"Hook contact lookup unavailable: route={route}, status={status}, error={type(exc).__name__}")
            return None

    def _queue(self, key):
        if self._closed or key in self._pending or key in self._inflight:
            return
        if self._misses.get(key, 0) > time.monotonic() or self.get(key):
            return
        if len(self._pending) + len(self._inflight) < 512:
            self._pending[key] = (time.monotonic(), 0)

    def _defer(self, keys, attempts):
        exhausted = 0
        for key in keys:
            attempt = attempts[key] + 1
            if attempt >= self.MAX_ATTEMPTS:
                if key[0] != "snapshot":
                    self.put(key, None, 60)
                    if key[0] == "user" and not self.get(key, allow_stale=True):
                        exhausted += 1
            else:
                self._pending[key] = (time.monotonic() + self.RETRY_DELAY * attempt, attempt)
        if exhausted:
            logger.warning(f"Hook nickname lookup deferred: native contact database remained idle; count={exhausted}; messages continue without waiting")

    async def _users(self, users):
        response = await self.lookup("/BatchGetContactBriefInfo", {"wxidlist": users})
        for row in rows_from(response):
            uid = field(row, "UserName", "wxid")
            name = display_name(field(row, "NickName", "display_name"))
            if uid in users and name and name != uid:
                self.put(("user", uid), name)
        unresolved = [uid for uid in users if not self.get(("user", uid))]
        # The 4.1.15 API has no generic QueryDB fallback.
        if unresolved and not self.native_contacts:
            for uid, name in (await self.database.resolve(unresolved)).items():
                name = display_name(name)
                if name and name != uid:
                    self.put(("user", uid), name)
            for uid in unresolved:
                if self.get(("user", uid)):
                    continue
                rows = rows_from(await self.lookup("/GetContact", {"wxid": uid}), 1)
                row = rows[0] if rows else {}
                name = display_name(field(row, "NickName", "display_name"))
                if field(row, "UserName", "wxid") == uid and name and name != uid:
                    self.put(("user", uid), name)
        resolved = sum(bool(self.get(("user", uid))) for uid in users)
        for uid in users:
            if not self.get(("user", uid)):
                self.put(("user", uid), "", 60)
        if resolved:
            logger.info(f"Hook display names resolved in background: count={resolved}")

    async def _group(self, room):
        rows = rows_from(await self.lookup("/GetContact", {"wxid": room}), 1)
        row = rows[0] if rows else {}
        if field(row, "UserName", "wxid") != room:
            self.put(("group", room), {}, 60)
            return
        members = row.get("members")
        names = {}
        for member in members[:4096] if isinstance(members, list) else []:
            uid = field(member, "wxid", "UserName")
            name = display_name(field(member, "nickname", "display_name"))
            if uid and name and name != uid:
                names[uid] = name
        if names and row.get("members_complete") is False:
            names = {**(self.get(("group", room), allow_stale=True) or {}), **names}
        self.put(("group", room), names, 3600 if names else 60)

    async def _snapshot(self):
        response = await self.lookup("/GetContacts", {"offset": 0, "limit": 4096})
        resolved = 0
        for row in rows_from(response, 4096):
            uid = field(row, "UserName", "wxid")
            name = display_name(field(row, "NickName", "display_name"))
            if uid and name and name != uid:
                self.put(("user", uid), name)
                resolved += 1
        if resolved:
            logger.info(f"Hook contact nickname snapshot cached: count={resolved}; limit=4096")

    async def _background(self):
        while self._pending and not self._closed:
            for key in list(self._pending):
                if key[0] in ("user", "group") and self.get(key):
                    self._pending.pop(key)
            if not self._pending:
                break
            now = time.monotonic()
            ready = [key for key, (when, _) in self._pending.items() if when <= now]
            if not ready:
                await asyncio.sleep(min(5, max(0, min(when for when, _ in self._pending.values()) - now)))
                continue
            users = [key for key in ready if key[0] == "user"][:200]
            keys = users or ready[:1]
            attempts = {key: self._pending.pop(key)[1] for key in keys}
            self._inflight.update(keys)
            try:
                if users:
                    await self._users([key[1] for key in users])
                elif keys[0][0] == "snapshot":
                    await self._snapshot()
                else:
                    await self._group(keys[0][1])
            except (ContactDeferred, httpx.HTTPError, TimeoutError):
                self._defer(keys, attempts)
            except (RuntimeError, ValueError, TypeError) as exc:
                logger.warning(f"Hook background nickname lookup failed ({type(exc).__name__}); messages continue without waiting")
                for key in keys:
                    self.put(key, None, 30)
            finally:
                self._inflight.difference_update(keys)
                self._flush_cache()

    async def resolve(self, messages):
        messages = list(messages)
        for msg in messages:
            known = display_name(msg.sender_name) if msg.sender_name != msg.sender else ""
            if known == "昵称未取得":
                known = ""
            if known:
                self.put(("user", msg.sender), known)
            if not known and msg.sender != "wechat_system" and not self.get(("user", msg.sender)):
                self._queue(("user", msg.sender))
                if msg.is_group:
                    self._queue(("group", msg.room))
        if self._pending and (self._task is None or self._task.done()) and not self._closed:
            self._task = asyncio.create_task(self._background(), name="wechat-contact-names")
        # Give new senders a short chance to resolve. Cached names can dispatch
        # immediately, even when a refresh is pending in the background.
        if self._task is not None and not self._task.done() and any(not self._name(msg) for msg in messages):
            await asyncio.wait({self._task}, timeout=self.FOREGROUND_WAIT)
        result = [replace(msg, sender_name=self._name(msg) or "昵称未取得") for msg in messages]
        self._flush_cache()
        return result

    def _name(self, msg):
        user = ("user", msg.sender)
        room = ("group", msg.room)
        fresh_group = (self.get(room) or {}).get(msg.sender) if msg.is_group else None
        known = msg.sender_name if msg.sender_name not in (msg.sender, "昵称未取得") else ""
        fresh = fresh_group or known or self.get(user)
        if fresh:
            return fresh
        if msg.sender == "wechat_system":
            return "微信系统"
        stale_group = (self.get(room, allow_stale=True) or {}).get(msg.sender) if msg.is_group else None
        return stale_group or self.get(user, allow_stale=True)

    async def aclose(self):
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._pending.clear()
        if self._store is not None:
            self._flush_cache()
            self._store.close()
            self._store = None

