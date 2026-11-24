"""AstrBot WebUI and command bridge for WeChat Hook login status & QR code scan."""
from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import urlsplit

import httpx
from astrbot.api import logger as log

try:
    from astrbot.api.web import error_response, json_response, request
    _WEB_AVAILABLE = True
except ImportError:
    error_response = json_response = request = None
    _WEB_AVAILABLE = False

from .maintenance import format_resume_time


PLUGIN_NAME = "astrbot_plugin_wechat_hook"
MAX_QR_BYTES = 512 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def sanitize_hook_origin(url: str) -> str:
    raw = str(url or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or any(ch.isspace() or ord(ch) < 32 for ch in raw)
        or (parsed.port is not None and not 1 <= parsed.port <= 65535)
    ):
        raise ValueError("Invalid hook_url origin")
    return raw


def _live_instance_map(context) -> dict[str, object]:
    manager = getattr(context, "platform_manager", None)
    if manager is None or not hasattr(manager, "get_insts"):
        return {}
    mapping: dict[str, object] = {}
    for inst in manager.get_insts() or ():
        meta = inst.meta() if hasattr(inst, "meta") else None
        pid = str(getattr(meta, "id", "") or "")
        if pid and getattr(meta, "name", "") == "wechat_pc_hook":
            mapping[pid] = inst
    return mapping


def list_hook_platforms(context) -> list[dict]:
    manager = getattr(context, "platform_manager", None)
    if manager is None:
        return []
    live_map = _live_instance_map(context)
    configs = list(getattr(manager, "platforms_config", None) or ())
    results: list[dict] = []
    seen: set[str] = set()
    for cfg in configs:
        if not isinstance(cfg, dict) or cfg.get("type") != "wechat_pc_hook":
            continue
        pid = str(cfg.get("id") or "wechat_pc_hook")
        seen.add(pid)
        inst = live_map.get(pid)
        try:
            origin = sanitize_hook_origin(cfg.get("hook_url") or "http://127.0.0.1:30001")
        except ValueError:
            origin = ""
        results.append({
            "id": pid,
            "enable": bool(cfg.get("enable")),
            "self_wxid": str(cfg.get("self_wxid") or ""),
            "hook_origin": origin,
            "running": inst is not None,
            "lifecycle_state": str(getattr(inst, "_lifecycle_state", "") or ("stopped" if inst is None else "active")),
        })
    for pid, inst in live_map.items():
        if pid in seen:
            continue
        cfg = getattr(inst, "config", {}) or {}
        try:
            origin = sanitize_hook_origin(cfg.get("hook_url") or "")
        except ValueError:
            origin = ""
        results.append({
            "id": pid,
            "enable": bool(cfg.get("enable", True)),
            "self_wxid": str(getattr(inst, "owner", "") or cfg.get("self_wxid") or ""),
            "hook_origin": origin,
            "running": True,
            "lifecycle_state": str(getattr(inst, "_lifecycle_state", "active") or "active"),
        })
    return results


def find_hook_platform(context, platform_id: str | None = None) -> tuple[dict | None, object | None]:
    manager = getattr(context, "platform_manager", None)
    if manager is None:
        return None, None
    live_map = _live_instance_map(context)
    target_id = str(platform_id or "").strip()
    candidates: list[dict] = [
        cfg for cfg in (getattr(manager, "platforms_config", None) or ())
        if isinstance(cfg, dict) and cfg.get("type") == "wechat_pc_hook"
    ]
    if target_id:
        for cfg in candidates:
            if str(cfg.get("id") or "wechat_pc_hook") == target_id:
                return cfg, live_map.get(target_id)
        if target_id in live_map:
            inst = live_map[target_id]
            return dict(getattr(inst, "config", {}) or {"id": target_id}), inst
        return None, None
    for cfg in candidates:
        if cfg.get("enable"):
            pid = str(cfg.get("id") or "wechat_pc_hook")
            return cfg, live_map.get(pid)
    if candidates:
        cfg = candidates[0]
        pid = str(cfg.get("id") or "wechat_pc_hook")
        return cfg, live_map.get(pid)
    if live_map:
        pid, inst = next(iter(live_map.items()))
        return dict(getattr(inst, "config", {}) or {"id": pid}), inst
    return None, None


def _handoff_resume_payload(live_inst) -> tuple[int, str, str]:
    if live_inst is None or not hasattr(live_inst, "get_maintenance_handoff"):
        return 0, "", ""
    handoff = live_inst.get_maintenance_handoff()
    if not isinstance(handoff, dict):
        return 0, "", ""
    resume_at = int(handoff.get("resume_at") or 0)
    return resume_at, format_resume_time(resume_at), str(handoff.get("window") or "")


async def _probe_hook_login_internal(
    config: dict,
    live_inst=None,
    *,
    include_qr: bool = True,
    client: httpx.AsyncClient | None = None,
) -> tuple[dict, bytes | None]:
    pid = str(config.get("id") or "wechat_pc_hook")
    wxid = str(config.get("self_wxid") or getattr(live_inst, "owner", "") or "")
    try:
        origin = sanitize_hook_origin(config.get("hook_url") or "http://127.0.0.1:30001")
    except ValueError:
        return {
            "platform_id": pid,
            "self_wxid": wxid,
            "hook_origin": "",
            "status": "invalid_config",
            "local_account_ready": False,
            "qrcode_available": False,
            "qr_data_url": None,
            "qr_sha256": "",
            "qr_busy": False,
            "resume_at": 0,
            "resume_at_text": "",
            "maintenance_window": "",
            "detail": "hook_url 配置格式无效，请检查适配器配置。",
        }, None

    token = str(config.get("hook_token") or "").strip()
    headers = {"X-Wxcx-Token": token} if token else {}

    async def _run(http_client: httpx.AsyncClient) -> tuple[dict, bytes | None]:
        base_result = {
            "platform_id": pid,
            "self_wxid": wxid,
            "hook_origin": origin,
            "status": "offline",
            "local_account_ready": False,
            "local_account_state": "",
            "qrcode_available": False,
            "qr_data_url": None,
            "qr_sha256": "",
            "qr_busy": False,
            "resume_at": 0,
            "resume_at_text": "",
            "maintenance_window": "",
            "detail": "",
        }
        try:
            state_resp = await http_client.get(f"{origin}/wxcx/login/state", headers=headers)
        except httpx.HTTPError:
            resume_at, resume_text, window = _handoff_resume_payload(live_inst)
            if resume_at > 0:
                base_result.update({
                    "status": "maintenance",
                    "resume_at": resume_at,
                    "resume_at_text": resume_text,
                    "maintenance_window": window,
                    "detail": (
                        f"微信处于计划停机维护中，预计恢复时间：{resume_text}。"
                        if resume_text else "微信处于计划停机维护中。"
                    ),
                })
            else:
                base_result["detail"] = "无法连接微信 Hook 接口，请确认微信与 Hook 已启动。"
            return base_result, None

        if state_resp.status_code == 404:
            base_result["status"] = "unsupported"
            base_result["detail"] = "当前微信 Hook 版本尚未提供 /wxcx/login/state 接口。"
            return base_result, None
        if state_resp.status_code in (401, 403):
            base_result["status"] = "auth_error"
            base_result["detail"] = "Hook 访问令牌鉴权失败，请核对 hook_token。"
            return base_result, None
        if state_resp.status_code == 429:
            base_result["status"] = "busy"
            base_result["qr_busy"] = True
            base_result["detail"] = "微信 Hook 正在采集中，请稍后刷新。"
            return base_result, None
        if state_resp.status_code >= 400:
            base_result["status"] = "error"
            base_result["detail"] = f"Hook 状态接口返回 HTTP {state_resp.status_code}。"
            return base_result, None

        try:
            payload = state_resp.json()
        except ValueError:
            base_result["status"] = "error"
            base_result["detail"] = "Hook 状态接口返回了非 JSON 响应。"
            return base_result, None
        if not isinstance(payload, dict):
            base_result["status"] = "error"
            base_result["detail"] = "Hook 状态响应格式异常。"
            return base_result, None

        screen = str(payload.get("login_screen_state") or "")
        ready = payload.get("local_account_ready") is True
        account_state = str(payload.get("local_account_state") or "")
        maint = payload.get("maintenance") if isinstance(payload.get("maintenance"), dict) else {}
        maint_active = maint.get("active") is True
        resume_at = int(maint.get("resume_at") or 0) if type(maint.get("resume_at")) is int else 0
        if resume_at <= 0:
            handoff_resume, _, handoff_window = _handoff_resume_payload(live_inst)
            resume_at = handoff_resume
            maint_window = handoff_window
        else:
            maint_window = str(maint.get("window") or "")
        resume_text = format_resume_time(resume_at)

        base_result["local_account_ready"] = ready
        base_result["local_account_state"] = account_state
        base_result["resume_at"] = resume_at
        base_result["resume_at_text"] = resume_text
        base_result["maintenance_window"] = maint_window

        if maint_active or screen == "maintenance":
            base_result["status"] = "maintenance"
            base_result["detail"] = (
                f"微信处于计划维护中，预计恢复时间：{resume_text}。"
                if resume_text else "微信处于计划维护中。"
            )
            return base_result, None

        if ready or screen == "logged_in":
            base_result["status"] = "logged_in"
            base_result["local_account_ready"] = True
            base_result["detail"] = "微信已登录在线，无需扫码。"
            return base_result, None

        if screen == "auto_login_button":
            base_result["status"] = "auto_login_button"
            base_result["detail"] = "微信当前停留在「确认登录」按钮页（可直接在微信端点击，或等待定时启动自动点击）。"
            return base_result, None

        if screen != "qr_code" and payload.get("qrcode_available") is not True:
            base_result["status"] = "unavailable"
            base_result["detail"] = "微信未就绪且暂未检测到二维码（可能在安全校验页或等待手机确认）。"
            return base_result, None

        base_result["status"] = "qr_code"
        base_result["qrcode_available"] = True
        base_result["detail"] = "检测到登录二维码：可直接扫描下方二维码，或在微信窗口扫码。"
        if not include_qr:
            return base_result, None

        try:
            qr_resp = await http_client.get(f"{origin}/wxcx/login/qrcode", headers=headers)
        except httpx.HTTPError:
            base_result["detail"] = "检测到二维码状态，但拉取二维码图片失败，请稍后重试。"
            return base_result, None

        if qr_resp.status_code == 409:
            base_result.update({
                "status": "logged_in",
                "local_account_ready": True,
                "qrcode_available": False,
                "detail": "微信刚刚已完成登录，无需再扫码。",
            })
            return base_result, None
        if qr_resp.status_code == 429:
            base_result["qr_busy"] = True
            base_result["detail"] = "二维码采集中，请稍等片刻后刷新。"
            return base_result, None
        if qr_resp.status_code != 200:
            base_result["detail"] = f"二维码暂时不可用（HTTP {qr_resp.status_code}），请稍后刷新。"
            return base_result, None

        raw_png = qr_resp.content
        ctype = str(qr_resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if (ctype and ctype != "image/png") or not raw_png.startswith(PNG_MAGIC) or len(raw_png) > MAX_QR_BYTES:
            base_result["detail"] = "Hook 返回的二维码图像无效或超出大小限制。"
            return base_result, None

        b64 = base64.b64encode(raw_png).decode("ascii")
        digest = hashlib.sha256(raw_png).hexdigest()[:12]
        base_result["qr_data_url"] = f"data:image/png;base64,{b64}"
        base_result["qr_sha256"] = digest
        return base_result, raw_png

    if client is not None:
        return await _run(client)
    async with httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False) as http_client:
        return await _run(http_client)


async def probe_hook_login(
    config: dict,
    live_inst=None,
    *,
    include_qr: bool = True,
    client: httpx.AsyncClient | None = None,
) -> dict:
    data, _ = await _probe_hook_login_internal(config, live_inst, include_qr=include_qr, client=client)
    return data


async def probe_hook_login_with_png(
    config: dict,
    live_inst=None,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[dict, bytes | None]:
    return await _probe_hook_login_internal(config, live_inst, include_qr=True, client=client)


class LoginPanel:
    def __init__(self, context):
        self.context = context
        self.active = True
        if _WEB_AVAILABLE and hasattr(context, "register_web_api"):
            context.register_web_api(
                f"/{PLUGIN_NAME}/login", self.handle, ["POST"], "微信扫码登录",
            )

    async def handle(self):
        if not _WEB_AVAILABLE:
            raise RuntimeError("astrbot.api.web is unavailable")
        if not self.active or not getattr(request, "username", None):
            return error_response("请先登录 AstrBot 后台并启用微信 Hook 插件。", status_code=403)
        try:
            raw = await request.body()
            if len(raw) > 8192:
                return error_response("请求体过大。", status_code=413)
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Expected JSON object")
        except (ValueError, TypeError):
            return error_response("请求参数格式错误。", status_code=400)

        operation = payload.get("operation")
        if operation == "list":
            return json_response(
                {"platforms": list_hook_platforms(self.context)},
                headers={"Cache-Control": "no-store"},
            )
        if operation == "status":
            platform_id = str(payload.get("platform_id") or "").strip()
            config, live_inst = find_hook_platform(self.context, platform_id)
            if config is None:
                return error_response("未找到已配置的微信 Hook 平台实例。", status_code=404)
            include_qr = payload.get("include_qr", True) is not False
            try:
                result = await probe_hook_login(config, live_inst, include_qr=include_qr)
                return json_response(result, headers={"Cache-Control": "no-store"})
            except Exception as exc:
                log.warning(f"LoginPanel probe failed: {type(exc).__name__}")
                return error_response("查询微信登录状态失败，请稍后重试。", status_code=502)
        return error_response("不支持的操作。", status_code=400)
