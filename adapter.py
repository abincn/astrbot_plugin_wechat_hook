"""AstrBot platform adapter for abincn PC WeChat Hook (pilot, safe by default).

Install this directory under AstrBot/data/plugins after installing httpx.
The existing WeChat Hook DLL/HTTP server is not modified.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from astrbot.api import logger as log
from astrbot.api.platform import (
    Platform, AstrBotMessage, MessageMember, MessageType, PlatformMetadata,
    register_platform_adapter,
)
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Image, Plain
from .events import Inbound
from .identity import split_session
from .inbox import DurableInbox
from .media import process_pending_media
from .delivery import attach_pipeline_ack
from .drafts import source_draft_identity
from .outbound import NativeTextSender, read_reply_image
from .components import inbound_content, mention_components
from .contacts import ContactNames
from .video_inputs import VideoInputs
from .file_artifacts import attach_file_metadata
from .responses_files import prepare_file_upload
from .notifications import service_notice_reason
from astrbot.core.platform.astr_message_event import current_message_event



def config_switch(value, name: str) -> bool:
    if type(value) is bool:
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError(f"{name} must be a boolean switch")


class HookResponseError(RuntimeError):
    def __init__(self, status_code: int, route: str, detail: str, error_code: str = ""):
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(f"Hook {route} HTTP {status_code}: {detail[:150]}")


class HookMessageEvent(AstrMessageEvent):
    def __init__(self, *args, adapter: "WeChatHookPlatform", **kwargs):
        super().__init__(*args, **kwargs)
        self.adapter = adapter

    async def send(self, message: MessageChain) -> None:
        await self.adapter.send_by_session(self.session, message, origin_event=self)
        await super().send(message)

    async def submit_generated_images(self, task_id: str, paths: list[str]) -> None:
        """Submit a completed ImageGen task through its original inbound event."""
        chain = MessageChain()
        for path in paths:
            chain.file_image(path)
        await self.adapter.send_by_session(
            self.session, chain, origin_event=self, generation_task_id=task_id,
        )

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
        "allow_remote_hook": False,
        "self_wxid": "",
        "inbox_db": "data/wechat_hook_inbox.sqlite3",
        "poll_interval": 2.0,
        "send_enabled": False,
        "capture_enabled": False,
        "video_frames_enabled": True,
        "video_audio_enabled": True,
    },
    support_streaming_message=False,
    adapter_display_name="微信 Hook 连接",
    config_metadata={
        "enable": {"type": "bool", "description": "启用连接"},
        "hook_url": {"type": "string", "description": "Hook 地址"},
        "hook_token": {"type": "string", "description": "Hook 访问令牌", "secret": True},
        "allow_remote_hook": {"type": "bool", "description": "允许远程 Hook", "hint": "Linux/Docker 跨主机连接时开启，必须填写令牌。仅使用可信网络/VPN或HTTPS；不会改变Windows Hook监听设置。"},
        "self_wxid": {"type": "string", "description": "登录微信的 wxid"},
        "inbox_db": {"type": "string", "description": "消息数据库路径"},
        "poll_interval": {"type": "float", "description": "轮询间隔（秒）"},
        "send_enabled": {"type": "bool", "description": "允许真实发送", "hint": "开启后，聊天回复和 AstrBot 后台发送都会提交给微信 Hook；历史草稿不会发送。"},
        "capture_enabled": {"type": "bool", "description": "自动开启 Hook 消息采集", "hint": "默认关闭，避免改动现有采集配置。"},
        "video_frames_enabled": {"type": "bool", "description": "视频画面分析", "hint": "使用自带 ffmpeg 最多抽取约 0、3、9 秒的三帧；需要视觉模型。"},
        "video_audio_enabled": {"type": "bool", "description": "视频音轨识别", "hint": "提取前最多120秒音轨，调用当前会话已配置的 ASR，可能产生识别费用；未配置时明确提示。"},
    },
)
class WeChatHookPlatform(Platform):
    def __init__(self, platform_config: dict, platform_settings: dict, event_queue: asyncio.Queue):
        super().__init__(platform_config, event_queue)
        self.owner = str(self.config.get("self_wxid") or "").strip()
        self._enabled = config_switch(self.config.get("enable", False), "enable")
        self.send_enabled = config_switch(self.config.get("send_enabled", False), "send_enabled")
        self.config.pop("proactive_targets", None)  # Removed after v0.6.12.
        self.capture_enabled = config_switch(self.config.get("capture_enabled", False), "capture_enabled")
        self.allow_remote_hook = config_switch(self.config.get("allow_remote_hook", False), "allow_remote_hook")
        self.video_frames_enabled = config_switch(self.config.get("video_frames_enabled", True), "video_frames_enabled")
        self.video_audio_enabled = config_switch(self.config.get("video_audio_enabled", True), "video_audio_enabled")
        self.config.update(enable=self._enabled, send_enabled=self.send_enabled,
                           capture_enabled=self.capture_enabled, video_frames_enabled=self.video_frames_enabled,
                           video_audio_enabled=self.video_audio_enabled, allow_remote_hook=self.allow_remote_hook)
        self.url = str(self.config.get("hook_url") or "http://127.0.0.1:30001").strip().rstrip("/")
        self.token = str(self.config.get("hook_token") or "").strip()
        try:
            endpoint = urlsplit(self.url)
            if (endpoint.scheme not in {'http', 'https'} or not endpoint.hostname
                    or endpoint.username is not None or endpoint.password is not None
                    or endpoint.path or endpoint.query or endpoint.fragment
                    or any(ch.isspace() or ord(ch) < 32 for ch in self.url)
                    or endpoint.port is not None and not 1 <= endpoint.port <= 65535):
                raise ValueError("invalid origin")
        except ValueError as exc:
            raise ValueError("hook_url must be an HTTP(S) origin such as http://192.168.1.10:30001; omit API paths, credentials and query parameters") from exc
        hostname = endpoint.hostname.lower().rstrip(".")
        is_loopback = hostname == 'localhost'
        try:
            is_loopback = is_loopback or ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            pass
        if not is_loopback:
            if not self.allow_remote_hook:
                raise ValueError("Enable '允许远程 Hook' (allow_remote_hook) to connect to another host, or use a loopback tunnel")
            if not self.token:
                raise ValueError("Remote Hook connections require hook_token")
        self.interval = max(0.5, float(self.config.get("poll_interval") or 2))
        self._inbox = DurableInbox(Path(str(self.config.get("inbox_db") or "data/wechat_hook_inbox.sqlite3")))
        self._inbox.reconcile_interrupted_media()
        self._inbox.reconcile_interrupted_pipeline()
        self._stopping = asyncio.Event()
        self._media_root = Path(str(self.config.get('inbox_db') or 'data/wechat_hook_inbox.sqlite3')).resolve().parent / 'media'
        self._client: httpx.AsyncClient | None = None
        self._task_runtime = None
        self._sender = NativeTextSender(self._inbox, self._request, self.owner)
        self._contacts = ContactNames(self._request)
        self._video_inputs = None
        self._meta = PlatformMetadata(
            name="wechat_pc_hook", description="PC WeChat Hook with persistent inbound audit",
            id=str(self.config.get("id") or "wechat_pc_hook"),
            support_streaming_message=False,
            support_proactive_message=self.send_enabled,
        )

    def meta(self) -> PlatformMetadata:
        return self._meta

    async def prepare_video_input(self, event, req, context) -> None:
        if self._stopping.is_set() or not (self.video_frames_enabled or self.video_audio_enabled):
            return
        if self._video_inputs is None:
            self._video_inputs = VideoInputs(self._media_root, context)
        await self._video_inputs.apply(event, req, frames_enabled=self.video_frames_enabled,
                                       audio_enabled=self.video_audio_enabled)

    async def prepare_file_upload(self, event, req, context) -> None:
        if self._stopping.is_set(): return
        if await prepare_file_upload(event, req, self._inbox, self.owner, self._media_root, context):
            log.info(f"Hook attachment prepared as Responses input_file: event={event.message_obj.message_id}; API acceptance pending")

    def get_task_scope(self) -> dict:
        """Versioned, data-only binding for an optional task plugin."""
        if self._stopping.is_set():
            raise ValueError("WeChat Hook adapter is stopped")
        return {"version": 2, "owner": self.owner,
                "inbox_db": str(self._inbox.location),
                "draft_only": not self.send_enabled}

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
        obj.type = MessageType.GROUP_MESSAGE if item.is_group else MessageType.FRIEND_MESSAGE
        obj.self_id = item.owner
        if item.is_group:
            obj.group_id = item.room
        obj.sender = MessageMember(user_id=item.sender, nickname=item.sender_name)
        obj.session_id = item.session_id
        obj.message_id = item.event_id
        obj.message_str, components = inbound_content(item)
        obj.timestamp = item.created_at
        obj.raw_message = item.raw
        # Only native Hook explicit @ metadata counts. Never infer @ from plain text.
        obj.message = mention_components(item)
        obj.message.extend(components)
        return obj

    def create_event(self, message: AstrBotMessage) -> AstrMessageEvent:
        event = HookMessageEvent(
            message_str=message.message_str, message_obj=message,
            platform_meta=self.meta(), session_id=message.session_id, adapter=self,
        )
        attach_pipeline_ack(event.trace, self._inbox, message.message_id)
        attach_file_metadata(event, self._inbox, self.owner)
        if message.type == MessageType.GROUP_MESSAGE:
            event.set_extra('wechat_workspace_id', self._inbox.workspace_id(message.self_id, message.group_id))
        return event

    async def _request(self, method: str, route: str, *, data: dict | None = None) -> tuple[int, dict | list]:
        assert self._client is not None
        resp = await self._client.request(method, route, json=data)
        if resp.status_code >= 400:
            error_code = ""
            try:
                error = resp.json()
                error_code = error.get("error", "") if isinstance(error, dict) else ""
            except ValueError:
                pass
            raise HookResponseError(resp.status_code, route, resp.text, error_code)
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
        self._inbox.ingest_batch(events, self.owner)
        for raw in events:
            reason = service_notice_reason(raw)
            if reason:
                log.info(f"Hook service notification retained; auto-reply skipped: reason={reason}")

    async def run(self) -> None:
        try:
            if not self._enabled:
                return
            if not self.owner:
                raise RuntimeError("Set self_wxid before enabling this adapter")
            headers = {"X-Wxcx-Token": self.token} if self.token else {}
            async with httpx.AsyncClient(
                base_url=self.url, headers=headers, timeout=8, trust_env=False, follow_redirects=False,
            ) as client:
                self._client = client
                if self.capture_enabled:
                    # Explicit opt-in. Never rewrite the Hook callback URL.
                    await self._request(
                        "POST", "/wxcx/messages/config",
                        data={"enabled": True, "self_id": self.owner},
                    )
                while not self._stopping.is_set():
                    try:
                        await self._poll_once()
                    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                        log.warning(f"Hook polling failed; preserving cursor: {exc}")
                    try:
                        await process_pending_media(
                            self._inbox, self._request, self._client, self._media_root,
                        )
                    except Exception as exc:
                        log.warning(f"Media processing failed: {type(exc).__name__}")
                    try:
                        await self._sender.poll_once()
                    except Exception as exc:
                        log.warning(f"Text status processing failed ({type(exc).__name__})")
                    # Existing durable pending events are dispatched without sending.
                    pending = await self._contacts.resolve(self._inbox.pending())
                    for msg in pending:
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
                              origin_event: HookMessageEvent | None = None, *,
                              generation_task_id: str | None = None) -> None:
        owner, room = split_session(session.session_id)
        is_group = room.endswith("@chatroom")
        expected_type = MessageType.GROUP_MESSAGE if is_group else MessageType.FRIEND_MESSAGE
        if session.message_type != expected_type:
            raise ValueError("Outbound session type differs from its WeChat destination")
        if owner != self.owner:
            raise ValueError("Outbound destination is outside configured account")
        parts = message_chain.chain if message_chain else []
        # Preserve AstrBot's At display text as well as the native atlist below.
        # An image-only reply with At needs this text message before its images.
        text_parts = []
        for part in parts:
            if isinstance(part, Plain):
                text_parts.append(part.text)
            elif isinstance(part, At):
                text_parts.append(f"@{part.name or part.qq}\u2005")
        text = "".join(text_parts).strip()
        images = [p for p in parts if isinstance(p, Image)]
        unsupported = [type(p).__name__ for p in parts if not isinstance(p, (Plain, At, Image))]
        if unsupported and self.send_enabled:
            raise ValueError("Native sending supports text, group @ and local PNG/JPEG images; unsupported: "
                             + ", ".join(unsupported))
        if unsupported:
            text += "\n[Unsupported outgoing components: " + ", ".join(unsupported) + "]"
        if not text.strip() and not images:
            return
        if self.send_enabled and (len(text) > 30000 or len(text.encode("utf-8")) > 32768):
            raise ValueError("Hook text exceeds 30000 characters or 32768 UTF-8 bytes; split the reply")
        mentions = []
        for part in parts:
            if isinstance(part, At):
                recipient = str(part.qq)
                if self.send_enabled and (not recipient or recipient.lower() in {"all", "0"}
                        or recipient.endswith("@chatroom") or len(recipient.encode("utf-8")) > 256
                        or any(ch.isspace() or ch in ",|:" for ch in recipient)):
                    raise ValueError("Unsupported native group @ target")
                if recipient not in mentions:
                    mentions.append(recipient)
        if self.send_enabled and len(mentions) > 64:
            raise ValueError("At most 64 native @ targets are supported")
        if self.send_enabled and mentions and not is_group:
            raise ValueError("Private text replies do not support group @ targets")
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
            if service_notice_reason(getattr(source.message_obj, "raw_message", None)):
                if origin_event is None:
                    raise PermissionError("WeChat service notification cannot trigger a send")
                log.info("Hook service notification reply suppressed; no native send requested")
                return
            source_event_id, intent_id = source_draft_identity(
                source, self, session.session_id, owner, room)
        if origin_event is None and not self.send_enabled:
            # Context.send_message() must not report a draft as a send.
            raise PermissionError("WeChat sending is disabled (send_enabled)")
        native_send = self.send_enabled and (origin_event is None or bool(source_event_id))
        if generation_task_id is not None:
            if not native_send:
                raise RuntimeError("ImageGen result requires native sending and its original WeChat event")
            key = f"{owner}\x1f{room}\x1f{source_event_id}\x1f{generation_task_id}"
            intent_id = "imagegen-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
        if native_send and (self._stopping.is_set() or self._client is None):
            raise RuntimeError("WeChat transport is not running")
        if native_send:
            # AstrBot and WeChat may run on different machines. Read the local
            # files before queuing a caption, then send bytes to the Hook.
            prepared = [await asyncio.to_thread(read_reply_image, p.path or p.file) for p in images]
            if text.strip():
                record_id = self._inbox.record_outbound(
                    owner, room, text, "send_prepared", source_event_id, intent_id,
                )
                await self._sender.send_once(record_id, room, text, mentions)
            for index, (data, name) in enumerate(prepared):
                record_id = self._inbox.record_outbound(
                    owner, room, "[Image]", "send_prepared", source_event_id,
                    f"{intent_id}-image-{index}" if intent_id else "",
                )
                await self._sender.send_image_once(record_id, room, data, name)
        else:
            if images:
                text = "\n".join(filter(None, [text, *(["[Image]"] * len(images))]))
            self._inbox.record_outbound(owner, room, text, "draft_only", source_event_id, intent_id)
            log.info(f"Draft persisted for {room}; no native send was requested")
        await super().send_by_session(session, message_chain)
