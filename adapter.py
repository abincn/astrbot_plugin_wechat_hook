"""AstrBot platform adapter for abincn PC WeChat Hook (pilot, safe by default).

Install this directory under AstrBot/data/plugins after installing httpx.
The existing WeChat Hook DLL/HTTP server is not modified.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from astrbot.api.platform import (
    Platform, AstrBotMessage, MessageMember, MessageType, PlatformMetadata,
    register_platform_adapter,
)
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Plain, Image
from .events import Inbound
from .identity import split_session
from .inbox import DurableInbox
from .media import process_pending_media
from .delivery import attach_pipeline_ack
from .drafts import source_draft_identity
from astrbot.core.platform.astr_message_event import current_message_event

log = logging.getLogger(__name__)


class HookMessageEvent(AstrMessageEvent):
    def __init__(self, *args, adapter: "WeChatHookPlatform", **kwargs):
        super().__init__(*args, **kwargs)
        self.adapter = adapter

    async def send(self, message: MessageChain) -> None:
        await self.adapter.send_by_session(self.session, message, origin_event=self)
        await super().send(message)

    def next_draft_index(self) -> int:
        # One reply may have several independently persisted chunks.
        # The index is scoped to the source event, not the entire group.
        n = getattr(self, "_draft_index", 0)
        self._draft_index = n + 1
        return n


@register_platform_adapter(
    "wechat_pc_hook",
    "PC WeChat 4.1 via the existing abincn WeChat-Hook HTTP API",
    default_config_tmpl={
        "id": "wechat_pc_hook",
        "enable": False,
        "hook_url": "http://127.0.0.1:30001",
        "hook_token": "",
        "self_wxid": "",
        "allowed_rooms": [],
        "inbox_db": "data/wechat_hook_inbox.sqlite3",
        "poll_interval": 2.0,
        "send_enabled": False,
        "capture_enabled": False,
    },
    support_streaming_message=False,
)
class WeChatHookPlatform(Platform):
    def __init__(self, platform_config: dict, platform_settings: dict, event_queue: asyncio.Queue):
        super().__init__(platform_config, event_queue)
        self.owner = str(self.config.get("self_wxid") or "").strip()
        self.rooms = set(self.config.get("allowed_rooms") or [])
        self.url = str(self.config.get("hook_url") or "http://127.0.0.1:30001").rstrip("/")
        endpoint = urlsplit(self.url)
        if (endpoint.scheme != 'http' or endpoint.hostname not in {'127.0.0.1', 'localhost', '::1'}
                or endpoint.username or endpoint.password or endpoint.path or endpoint.query or endpoint.fragment):
            raise ValueError('hook_url must be a bare loopback HTTP origin')
        self.token = str(self.config.get("hook_token") or "")
        self.interval = max(0.5, float(self.config.get("poll_interval") or 2))
        self._inbox = DurableInbox(Path(str(self.config.get("inbox_db") or "data/wechat_hook_inbox.sqlite3")))
        self._inbox.reconcile_interrupted_media()
        self._inbox.reconcile_interrupted_pipeline()
        self._stopping = asyncio.Event()
        self._media_root = Path(str(self.config.get('inbox_db') or 'data/wechat_hook_inbox.sqlite3')).resolve().parent / 'media'
        self._client: httpx.AsyncClient | None = None
        self._task_runtime = None
        self._meta = PlatformMetadata(
            name="wechat_pc_hook", description="PC WeChat Hook with persistent inbound audit",
            id=str(self.config.get("id") or "wechat_pc_hook"),
            support_streaming_message=False, support_proactive_message=False,
        )

    def meta(self) -> PlatformMetadata:
        return self._meta

    def get_task_scope(self) -> dict:
        """Versioned, data-only binding for an optional task plugin."""
        if self._stopping.is_set():
            raise ValueError("WeChat Hook adapter is stopped")
        return {"version": 1, "owner": self.owner, "rooms": tuple(self.rooms),
                "inbox_db": str(self._inbox.location),
                "draft_only": not bool(self.config.get("send_enabled", False))}

    def attach_task_runtime(self, runtime) -> None:
        """The connector only owns stop supervision, never task construction."""
        self.get_task_scope()
        if self._task_runtime is not None:
            raise RuntimeError("WeChat Hook adapter already has a task runtime")
        self._task_runtime = runtime

    def detach_task_runtime(self, runtime) -> None:
        if self._task_runtime is runtime:
            self._task_runtime = None

    def to_astrbot(self, item: Inbound) -> AstrBotMessage:
        obj = AstrBotMessage()
        obj.type = MessageType.GROUP_MESSAGE
        obj.self_id = item.owner
        obj.group_id = item.room
        obj.sender = MessageMember(user_id=item.sender, nickname=item.sender)
        obj.session_id = item.session_id
        obj.message_id = item.event_id
        obj.message_str = "[图片]" if item.media_path else item.content
        obj.timestamp = item.created_at
        obj.raw_message = item.raw
        # Only native Hook explicit @ metadata counts. Never infer @ from plain text.
        obj.message = ([At(qq=item.owner)] if item.at_me is True else []) + [Plain(text=obj.message_str)]
        if item.media_path:
            obj.message.append(Image.fromFileSystem(item.media_path))
        return obj

    def create_event(self, message: AstrBotMessage) -> AstrMessageEvent:
        event = HookMessageEvent(
            message_str=message.message_str, message_obj=message,
            platform_meta=self.meta(), session_id=message.session_id, adapter=self,
        )
        attach_pipeline_ack(event.trace, self._inbox, message.message_id)
        event.set_extra('wechat_workspace_id', self._inbox.workspace_id(message.self_id, message.group_id))
        return event

    async def _request(self, method: str, route: str, *, data: dict | None = None) -> tuple[int, dict | list]:
        assert self._client is not None
        resp = await self._client.request(method, route, json=data)
        if resp.status_code >= 400:
            raise RuntimeError(f"Hook {route} HTTP {resp.status_code}: {resp.text[:150]}")
        payload = resp.json()
        if not isinstance(payload, (dict, list)):
            raise RuntimeError("Unexpected Hook response")
        return resp.status_code, payload

    async def _poll_once(self) -> None:
        _, status = await self._request("GET", "/wxcx/messages/status")
        if not isinstance(status, dict):
            raise RuntimeError("Hook message status response has no object")
        if status.get("enabled") is False or (isinstance(status.get("source"), dict) and status["source"].get("enabled") is False):
            raise RuntimeError("Hook message capture disabled: enable on the original Node console or opt into capture_enabled")
        cursor = self._inbox.after()
        try:
            _, payload = await self._request("GET", f"/wxcx/messages/events?after={cursor}&limit=128")
        except RuntimeError as exc:
            if "invalid_after" in str(exc).lower() or "ahead" in str(exc).lower():
                self._inbox.reset_cursor()
                log.warning("Hook cursor invalid after restart; reset to zero")
                return
            raise
        events = payload if isinstance(payload, list) else payload.get("events", [])
        if not isinstance(events, list):
            raise RuntimeError("Hook event response has no events list")
        self._inbox.ingest_batch(events, self.owner, self.rooms)

    async def run(self) -> None:
        try:
            if not self.owner or not self.rooms:
                raise RuntimeError("Set self_wxid and allowed_rooms before enabling this adapter")
            if not all(str(room).endswith("@chatroom") for room in self.rooms):
                raise RuntimeError("allowed_rooms must contain @chatroom group IDs")
            headers = {"X-Wxcx-Token": self.token} if self.token else {}
            async with httpx.AsyncClient(
                base_url=self.url, headers=headers, timeout=8, trust_env=False,
            ) as client:
                self._client = client
                if self.config.get("capture_enabled", False):
                    # Explicit opt-in. Never rewrite the Hook callback URL.
                    await self._request(
                        "POST", "/wxcx/messages/config",
                        data={"enabled": True, "self_id": self.owner},
                    )
                while not self._stopping.is_set():
                    try:
                        await self._poll_once()
                    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                        log.warning("Hook polling failed; preserving cursor: %s", exc)
                    try:
                        await process_pending_media(
                            self._inbox, self._request, self._client, self._media_root,
                        )
                    except Exception as exc:
                        log.warning('Media processing failed: %s', type(exc).__name__)
                    # Existing durable pending events are dispatched without sending.
                    for msg in self._inbox.pending():
                        try:
                            event = self.to_event(msg)
                            # Reserve durable state before commit_event's put_nowait.
                            # Otherwise fast pipeline completion can race the DB update.
                            if not self._inbox.mark_dispatched(msg.event_id):
                                continue
                            try:
                                self.commit_event(event)
                            except Exception as exc:
                                # Ambiguous queue acceptance: never auto-replay.
                                self._inbox.mark_dispatch_uncertain(msg.event_id, str(exc))
                                break
                        except Exception as exc:
                            self._inbox.mark_error(msg.event_id, str(exc))
                            break
                    try:
                        await asyncio.wait_for(self._stopping.wait(), timeout=self.interval)
                    except asyncio.TimeoutError:
                        pass
        finally:
            self._stopping.set()
            if self._task_runtime is not None:
                await self._task_runtime.stop()
            self._client = None
            self._inbox.close()

    def to_event(self, item: Inbound) -> AstrMessageEvent:
        return self.create_event(self.to_astrbot(item))

    async def terminate(self) -> None:
        self._stopping.set()
        if self._task_runtime is not None:
            await self._task_runtime.stop()

    async def send_by_session(self, session, message_chain: MessageChain,
                              origin_event: HookMessageEvent | None = None) -> None:
        owner, room = split_session(session.session_id)
        if owner != self.owner or room not in self.rooms:
            raise ValueError("Outbound destination is outside configured account/group")
        parts = message_chain.chain if message_chain else []
        text = "".join(p.text for p in parts if isinstance(p, Plain)).strip()
        unsupported = [type(p).__name__ for p in parts if not isinstance(p, Plain)]
        if unsupported:
            text += "\n[Unsupported outgoing components: " + ", ".join(unsupported) + "]"
        if not text.strip():
            return
        # An explicit HookMessageEvent.send() carries its own provenance.
        # For implicit context binding, require that AstrBot is *still executing*
        # the source event. Background asyncio tasks inherit ContextVars and may
        # retain a stale current_message_event after the pipeline has finished.
        source = origin_event if origin_event is not None else current_message_event.get()
        context_is_live = (origin_event is not None
                           or getattr(source, "_execution_active", False) is True)
        source_event_id = ""
        intent_id = ""
        if context_is_live and isinstance(source, HookMessageEvent):
            source_event_id, intent_id = source_draft_identity(
                source, self, session.session_id, owner, room)
        # Never invoke the native Hook send endpoint in this pilot.
        self._inbox.record_outbound(owner, room, text, "draft_only",
                                    source_event_id=source_event_id, intent_id=intent_id)
        log.info("Draft persisted for %s; native WeChat send disabled in prototype", room)
        await super().send_by_session(session, message_chain)
