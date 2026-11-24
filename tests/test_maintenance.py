import asyncio
import base64
from pathlib import Path
import importlib.util
import json
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

import httpx

SOURCE_DIR = Path(__file__).resolve().parents[1]


def _install_astrbot_stubs():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        **{level: lambda *a, **k: None for level in ("info", "warning", "debug", "error", "exception")}
    )

    platform = types.ModuleType("astrbot.api.platform")

    class Platform:
        def __init__(self, platform_config, event_queue):
            self.config = dict(platform_config)
            self.event_queue = event_queue
            self.last_error = None

        def record_error(self, message):
            self.last_error = types.SimpleNamespace(message=message)

        def clear_errors(self):
            self.last_error = None

        def commit_event(self, event):
            self.event_queue.put_nowait(event)

        async def send_by_session(self, session, message_chain):
            return None

    class PlatformMetadata:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class AstrBotMessage:
        pass

    class MessageMember:
        def __init__(self, user_id="", nickname=""):
            self.user_id = user_id
            self.nickname = nickname

    class MessageType:
        GROUP_MESSAGE = "group"
        FRIEND_MESSAGE = "friend"

    def register_platform_adapter(*args, **kwargs):
        return lambda cls: cls

    platform.Platform = Platform
    platform.AstrBotMessage = AstrBotMessage
    platform.MessageMember = MessageMember
    platform.MessageType = MessageType
    platform.PlatformMetadata = PlatformMetadata
    platform.register_platform_adapter = register_platform_adapter

    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
            self.trace = {}
            self.extras = {}

        def set_extra(self, k, v):
            self.extras[k] = v

        async def send(self, message):
            return None

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain or [])

        def file_image(self, path):
            self.chain.append(Image(file=path))

    event_filter = types.SimpleNamespace(
        command=lambda *a, **k: (lambda fn: fn),
        on_llm_request=lambda *a, **k: (lambda fn: fn),
    )
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.filter = event_filter

    star_mod = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

    star_mod.Context = object
    star_mod.Star = Star

    web_mod = types.ModuleType("astrbot.api.web")
    web_mod.request = types.SimpleNamespace(username="admin", _raw=b"{}")

    async def _req_body():
        return web_mod.request._raw

    web_mod.request.body = _req_body
    web_mod.json_response = lambda data, status_code=200, headers=None: {
        "status": status_code,
        "data": data,
        "headers": dict(headers or {}),
    }
    web_mod.error_response = lambda message, status_code=400, headers=None: {
        "status": status_code,
        "error": message,
        "headers": dict(headers or {}),
    }

    comp_mod = types.ModuleType("astrbot.api.message_components")

    class _BaseComp:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Plain(_BaseComp):
        def __init__(self, text=""):
            super().__init__(text=text)

    for name in ("At", "AtAll", "File", "Image", "Json", "Location", "Node", "Nodes", "Record", "Reply", "Share", "Video"):
        setattr(comp_mod, name, type(name, (_BaseComp,), {}))
    comp_mod.Plain = Plain

    core_mod = types.ModuleType("astrbot.core")
    core_platform = types.ModuleType("astrbot.core.platform")
    core_event = types.ModuleType("astrbot.core.platform.astr_message_event")
    import contextvars
    core_event.current_message_event = contextvars.ContextVar("current_message_event", default=None)

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.platform"] = platform
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.web"] = web_mod
    sys.modules["astrbot.api.message_components"] = comp_mod
    sys.modules["astrbot.core"] = core_mod
    sys.modules["astrbot.core.platform"] = core_platform
    sys.modules["astrbot.core.platform.astr_message_event"] = core_event


_install_astrbot_stubs()

PKG_NAME = "astrbot_plugin_wechat_hook_test"
pkg = types.ModuleType(PKG_NAME)
pkg.__path__ = [str(SOURCE_DIR)]
sys.modules[PKG_NAME] = pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(f"{PKG_NAME}.{name}", SOURCE_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{PKG_NAME}.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


inbox_mod = _load("inbox")
maintenance = _load("maintenance")
adapter_mod = _load("adapter")
login_panel_mod = _load("login_panel")
main_mod = _load("main")

from astrbot.api import web as web_stub
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.platform import MessageType


FAKE_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRfake_qr_payload"


class MaintenanceHandoffTests(unittest.TestCase):
    def test_parse_and_check_handoff_bounds(self):
        now = 1_700_000_000
        self.assertIsNone(maintenance.parse_maintenance_handoff(None, now_ts=now))
        self.assertIsNone(maintenance.parse_maintenance_handoff({"active": False, "resume_at": now + 3600}, now_ts=now))
        self.assertIsNone(maintenance.parse_maintenance_handoff({"active": True, "resume_at": now - 10}, now_ts=now))
        self.assertIsNone(maintenance.parse_maintenance_handoff({"active": True, "resume_at": now + 48 * 3600}, now_ts=now))
        self.assertIsNone(maintenance.parse_maintenance_handoff({"active": True, "resume_at": True}, now_ts=now))

        handoff = maintenance.parse_maintenance_handoff(
            {"active": True, "phase": "draining", "window": "00:30-06:30", "reason": "nightly_restart", "resume_at": now + 3600},
            now_ts=now,
        )
        self.assertIsNotNone(handoff)
        self.assertTrue(maintenance.is_handoff_active(handoff, now_ts=now + 10))
        self.assertFalse(maintenance.is_handoff_active(handoff, now_ts=now + 3600))
        self.assertTrue(len(maintenance.format_resume_time(handoff["resume_at"])) >= 10)

    def test_inbox_runtime_state_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "inbox.sqlite3"
            inbox = inbox_mod.DurableInbox(db_path)
            self.assertIsNone(inbox.get_state("maintenance_handoff"))
            inbox.set_state("maintenance_handoff", {"resume_at": 1_800_000_000, "phase": "offline"})
            inbox.close()

            reopened = inbox_mod.DurableInbox(db_path)
            try:
                loaded = reopened.get_state("maintenance_handoff")
                self.assertEqual(loaded["resume_at"], 1_800_000_000)
                reopened.set_state("maintenance_handoff", None)
                self.assertIsNone(reopened.get_state("maintenance_handoff"))
            finally:
                reopened.close()

    def test_classify_poll_error(self):
        maint_exc = adapter_mod.HookResponseError(503, "/wxcx/messages/status", "maintenance", "wechat_maintenance")
        pending_exc = adapter_mod.HookResponseError(503, "/wxcx/messages/status", "pending", "wechat_login_pending")
        conn_exc = httpx.ConnectError("connection refused")
        self.assertEqual(maintenance.classify_poll_error(maint_exc, maintenance_expected=False), "maintenance")
        self.assertEqual(maintenance.classify_poll_error(pending_exc, maintenance_expected=True), "maintenance")
        self.assertEqual(maintenance.classify_poll_error(conn_exc, maintenance_expected=True), "maintenance")
        self.assertEqual(maintenance.classify_poll_error(pending_exc, maintenance_expected=False), "login_required")
        self.assertEqual(maintenance.classify_poll_error(conn_exc, maintenance_expected=False), "error")


class AdapterAndLoginPanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _make_platform(self, **overrides):
        cfg = {
            "id": "wechat_test",
            "type": "wechat_pc_hook",
            "enable": True,
            "hook_url": "http://127.0.0.1:30002",
            "hook_token": "super_secret_token",
            "self_wxid": "wxid_bot",
            "inbox_db": str(Path(self.tmpdir.name) / "inbox.sqlite3"),
            "poll_interval": 0.5,
            "send_enabled": True,
            "capture_enabled": False,
            "maintenance_window_enabled": True,
            "maintenance_stop_time": "00:30",
            "maintenance_start_time": "06:30",
            "maintenance_timezone": "Asia/Shanghai",
            "maintenance_grace_minutes": 5,
        }
        cfg.update(overrides)
        plat = adapter_mod.WeChatHookPlatform(cfg, {}, asyncio.Queue())
        self.addAsyncCleanup(plat.terminate)
        return plat

    async def test_handoff_persists_across_disconnect_and_clears_on_recovery(self):
        plat = self._make_platform(poll_interval=0.05)
        self.assertNotIn("maintenance_stop_time", plat.config)
        future_resume = int(time.time()) + 3600
        phase = {"step": 0}
        media_calls = 0

        async def fake_media(*args, **kwargs):
            nonlocal media_calls
            media_calls += 1

        async def fake_sender_poll():
            return None

        async def fast_wait(coro, timeout=None):
            coro.close()
            raise asyncio.TimeoutError()

        async def fake_request(method, route, **kwargs):
            step = phase["step"]
            if step == 0:
                phase["step"] = 1
                return 200, {
                    "enabled": True,
                    "maintenance": {
                        "active": True,
                        "phase": "draining",
                        "window": "00:30-06:30",
                        "resume_at": future_resume,
                    },
                }
            if step == 1:
                phase["step"] = 2
                raise httpx.ConnectError("wechat exited cleanly")
            if step == 2:
                phase["step"] = 3
                raise adapter_mod.HookResponseError(
                    503, route, "starting", "wechat_maintenance",
                    payload={"error": "wechat_maintenance", "maintenance": {"active": True, "phase": "starting", "resume_at": future_resume}},
                )
            if route == "/wxcx/messages/status":
                return 200, {"enabled": True, "maintenance": {"active": False, "phase": "none"}}
            if route == "/wxcx/login/state":
                return 200, {"local_account_ready": True, "login_screen_state": "logged_in"}
            if route.startswith("/wxcx/messages/events"):
                plat._stopping.set()
                return 200, {"events": []}
            raise AssertionError(f"unexpected route {route}")

        plat._request = fake_request
        plat._sender.poll_once = fake_sender_poll

        with patch.object(adapter_mod, "process_pending_media", fake_media), \
             patch.object(adapter_mod.asyncio, "wait_for", side_effect=fast_wait):
            await plat.run()

        self.assertEqual(plat._lifecycle_state, "active")
        self.assertIsNone(plat.last_error)
        self.assertIsNone(plat.get_maintenance_handoff())
        self.assertEqual(media_calls, 1)

    async def test_expired_handoff_surfaces_error_on_connect_refused(self):
        plat = self._make_platform()
        past_resume = int(time.time()) - 60
        plat._inbox.set_state("maintenance_handoff", {"resume_at": past_resume, "phase": "offline"})
        plat._maintenance_handoff = plat._inbox.get_state("maintenance_handoff")
        kind = maintenance.classify_poll_error(
            httpx.ConnectError("refused"),
            maintenance_expected=plat._is_maintenance_expected(),
        )
        self.assertEqual(kind, "error")
        self.assertIsNone(plat._inbox.get_state("maintenance_handoff"))

        future_resume = int(time.time()) + 1800
        plat._record_maintenance_handoff({"active": True, "phase": "offline", "resume_at": future_resume})
        self.assertIsNotNone(plat.get_maintenance_handoff())

        plat._client = object()
        plat._enter_lifecycle_state("maintenance")
        session = types.SimpleNamespace(session_id="wxid_bot|123@chatroom", message_type=MessageType.GROUP_MESSAGE)
        with self.assertRaisesRegex(RuntimeError, "scheduled maintenance window"):
            await plat.send_by_session(session, MessageChain([Plain("hello")]))

    async def test_login_panel_list_status_qr_and_auth(self):
        plat = self._make_platform()
        cfg = {
            "id": "wechat_test",
            "type": "wechat_pc_hook",
            "enable": True,
            "hook_url": "http://127.0.0.1:30002",
            "hook_token": "super_secret_token",
            "self_wxid": "wxid_bot",
        }
        registered = []
        context = types.SimpleNamespace(
            platform_manager=types.SimpleNamespace(
                platforms_config=[cfg],
                get_insts=lambda: [plat],
            ),
            register_web_api=lambda route, handler, methods, desc: registered.append((route, methods, desc)),
        )
        panel = login_panel_mod.LoginPanel(context)
        self.assertEqual(registered[0][0], "/astrbot_plugin_wechat_hook/login")

        web_stub.request.username = ""
        web_stub.request._raw = json.dumps({"operation": "list"}).encode("utf-8")
        unauth = await panel.handle()
        self.assertEqual(unauth["status"], 403)

        web_stub.request.username = "admin"
        listed = await panel.handle()
        self.assertEqual(listed["status"], 200)
        dumped = json.dumps(listed["data"])
        self.assertNotIn("super_secret_token", dumped)
        self.assertEqual(listed["data"]["platforms"][0]["id"], "wechat_test")

        def qr_handler(req: httpx.Request):
            self.assertEqual(req.headers.get("X-Wxcx-Token"), "super_secret_token")
            if req.url.path == "/wxcx/login/state":
                return httpx.Response(200, json={
                    "local_account_ready": False,
                    "local_account_state": "wait_scan",
                    "login_screen_state": "qr_code",
                    "qrcode_available": True,
                    "maintenance": {"active": False},
                })
            if req.url.path == "/wxcx/login/qrcode":
                return httpx.Response(200, content=FAKE_PNG, headers={"content-type": "image/png"})
            raise AssertionError(req.url)

        async with httpx.AsyncClient(transport=httpx.MockTransport(qr_handler)) as mock_client:
            res = await login_panel_mod.probe_hook_login(cfg, plat, client=mock_client)
        self.assertEqual(res["status"], "qr_code")
        self.assertEqual(res["qr_data_url"], "data:image/png;base64," + base64.b64encode(FAKE_PNG).decode("ascii"))
        self.assertNotIn("super_secret_token", json.dumps(res))

        def logged_handler(req: httpx.Request):
            if req.url.path == "/wxcx/login/state":
                return httpx.Response(200, json={"local_account_ready": False, "login_screen_state": "qr_code", "qrcode_available": True})
            return httpx.Response(409, json={"error": "wechat_already_logged_in"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(logged_handler)) as mock_client:
            res_logged = await login_panel_mod.probe_hook_login(cfg, plat, client=mock_client)
        self.assertEqual(res_logged["status"], "logged_in")

        plat._record_maintenance_handoff({"active": True, "phase": "offline", "window": "00:30-06:30", "resume_at": int(time.time()) + 1200})

        def offline_handler(req: httpx.Request):
            raise httpx.ConnectError("down", request=req)

        async with httpx.AsyncClient(transport=httpx.MockTransport(offline_handler)) as mock_client:
            res_maint = await login_panel_mod.probe_hook_login(cfg, plat, client=mock_client)
        self.assertEqual(res_maint["status"], "maintenance")
        self.assertGreater(res_maint["resume_at"], int(time.time()))

    async def test_hook_qrcode_command(self):
        plat = self._make_platform()
        cfg = {
            "id": "wechat_cmd_test",
            "type": "wechat_pc_hook",
            "enable": True,
            "hook_url": "http://127.0.0.1:30002",
            "hook_token": "tok",
            "self_wxid": "wxid_bot",
        }
        context = types.SimpleNamespace(
            platform_manager=types.SimpleNamespace(
                platforms_config=[cfg],
                get_insts=lambda: [plat],
            ),
        )
        plugin = main_mod.Main(context)
        non_admin_evt = types.SimpleNamespace(
            get_platform_name=lambda: "wechat_pc_hook",
            role="member",
            plain_result=lambda t: ("plain", t),
            image_result=lambda p: ("image", p),
        )
        denied = []
        async for item in plugin.hook_qrcode(non_admin_evt):
            denied.append(item)
        self.assertEqual(len(denied), 1)
        self.assertIn("仅允许", denied[0][1])

        admin_evt = types.SimpleNamespace(
            get_platform_name=lambda: "webchat",
            role="member",
            plain_result=lambda t: ("plain", t),
            image_result=lambda p: ("image", p),
        )
        with patch.object(main_mod, "probe_hook_login_with_png", return_value=({"platform_id": "wechat_cmd_test", "status": "qr_code"}, FAKE_PNG)):
            outputs = []
            async for item in plugin.hook_qrcode(admin_evt):
                outputs.append(item)
        self.assertEqual(outputs[0][0], "image")
        qr_file = Path(outputs[0][1])
        self.assertTrue(qr_file.exists())
        self.assertEqual(qr_file.read_bytes(), FAKE_PNG)
        self.assertEqual(outputs[1][0], "plain")

        with patch.object(main_mod, "probe_hook_login_with_png", return_value=({"platform_id": "wechat_cmd_test", "status": "logged_in", "detail": "微信已登录在线，无需扫码。"}, None)):
            done = []
            async for item in plugin.hook_qrcode(admin_evt):
                done.append(item)
        self.assertFalse(qr_file.exists())
        self.assertIn("已登录在线", done[0][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
