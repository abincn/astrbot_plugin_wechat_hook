"""Single-source maintenance handoff (resume_at) and login-state helpers."""
from __future__ import annotations

from datetime import datetime
import time

import httpx


MAX_HANDOFF_HORIZON_SEC = 24 * 3600


def extract_hook_maintenance(payload: object) -> dict | None:
    if not isinstance(payload, dict):
        return None
    maint = payload.get("maintenance")
    if isinstance(maint, dict) and maint.get("active") is True:
        return maint
    return None


def parse_maintenance_handoff(maint: dict | None, *, now_ts: int | None = None) -> dict | None:
    if not isinstance(maint, dict) or maint.get("active") is not True:
        return None
    now_val = int(time.time()) if now_ts is None else int(now_ts)
    resume_raw = maint.get("resume_at")
    if type(resume_raw) is not int or resume_raw <= now_val or resume_raw > now_val + MAX_HANDOFF_HORIZON_SEC:
        return None
    return {
        "resume_at": resume_raw,
        "phase": str(maint.get("phase") or "offline"),
        "window": str(maint.get("window") or ""),
        "reason": str(maint.get("reason") or ""),
        "recorded_at": now_val,
    }


def is_handoff_active(handoff: dict | None, *, now_ts: int | None = None) -> bool:
    if not isinstance(handoff, dict):
        return False
    resume_at = handoff.get("resume_at")
    if type(resume_at) is not int or resume_at <= 0:
        return False
    now_val = int(time.time()) if now_ts is None else int(now_ts)
    return now_val < resume_at and resume_at <= now_val + MAX_HANDOFF_HORIZON_SEC


def format_resume_time(resume_at: int | None) -> str:
    if type(resume_at) is not int or resume_at <= 0:
        return ""
    return datetime.fromtimestamp(resume_at).astimezone().strftime("%Y-%m-%d %H:%M")


def classify_poll_error(exc: Exception, *, maintenance_expected: bool) -> str:
    status_code = getattr(exc, "status_code", None)
    error_code = str(getattr(exc, "error_code", "") or "")
    if status_code == 503 and error_code == "wechat_maintenance":
        return "maintenance"
    if maintenance_expected and (
        isinstance(exc, httpx.HTTPError)
        or (status_code == 503 and error_code == "wechat_login_pending")
    ):
        return "maintenance"
    if not maintenance_expected and status_code == 503 and error_code == "wechat_login_pending":
        return "login_required"
    return "error"


def format_login_required_notice(hook_url: str, login_state: dict | None) -> str:
    base = (hook_url or "").rstrip("/")
    screen = ""
    qr_url = "/wxcx/login/qrcode"
    qr_available = False
    if isinstance(login_state, dict):
        screen = str(login_state.get("login_screen_state") or "")
        qr_url = str(login_state.get("qrcode_url") or "/wxcx/login/qrcode")
        qr_available = login_state.get("qrcode_available") is True

    if screen == "qr_code" or qr_available:
        return (
            "微信需要扫码登录：可直接在微信窗口扫码，或打开 AstrBot「插件 -> 微信 Hook 连接 -> 可视化视图 -> 微信扫码登录」/ 发送 /hook_qrcode 获取二维码"
            f"（Hook 地址：{base}{qr_url}）。"
        )
    if screen == "auto_login_button":
        return "微信停留在确认登录页（可快速点登录按钮）；请在微信端确认登录，或前往 AstrBot「微信扫码登录」视图查看状态。"
    return "微信处于未登录状态；请在微信端确认是否需要扫码或滑块验证，也可在 AstrBot「微信扫码登录」视图刷新查看。"
