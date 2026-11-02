"""Distinguish direct service pushes from user chat without guessing from titles."""
from .payloads import xml_body


def service_notice_reason(raw: dict) -> str:
    if (not isinstance(raw, dict) or raw.get("direction") != "incoming"
            or raw.get("is_group") is not False):
        return ""
    sender = raw.get("sender_id")
    if sender in ("newsapp", "weixin", "weixinreminder", "qqmail"):
        return "builtin_service_account"
    if isinstance(sender, str) and sender.startswith("gh_"):
        return "official_account"
    content = raw.get("content") or raw.get("raw_content")
    if not isinstance(content, str) or not content.lstrip("\ufeff \t\r\n").startswith("<"):
        return ""
    root = xml_body(content)
    # Inspect only the direct envelope, never a quoted/forwarded appmsg payload.
    if root is not None and (root.tag == "mmreader" or root.tag == "msg" and root.find("mmreader") is not None):
        return "mmreader_service_push"
    return ""
