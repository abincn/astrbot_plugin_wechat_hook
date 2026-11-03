"""Bounded metadata extraction; never fetch card URLs or expand remote forwards."""
from __future__ import annotations

import re
import math
from urllib.parse import urlsplit, urlunsplit
import xml.etree.ElementTree as ET


def text(value, limit: int = 2000) -> str:
    return value[:limit] if isinstance(value, str) else ""


def xml_body(content: str):
    if len(content.encode("utf-8")) > 96000 or re.search(r"<!\s*(DOCTYPE|ENTITY)", content, re.I):
        return None
    try:
        root = ET.fromstring(content.lstrip("\ufeff \t\r\n"))
    except (ET.ParseError, ValueError):
        return None
    pending, count = [(root, 0)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > 1200 or depth > 24:
            return None
        pending.extend((child, depth + 1) for child in node)
    return root


def http_url(value) -> str:
    value = text(value, 4000)
    try:
        parsed = urlsplit(value)
        if (parsed.scheme in {"http", "https"} and parsed.hostname
                and not parsed.username and not parsed.password
                and not any(ord(c) < 32 for c in value)):
            return value
    except ValueError:
        pass
    return ""


def emoji_source(content: str) -> tuple[str, str]:
    root = xml_body(content)
    node = root if root is not None and root.tag == "emoji" else root.find("emoji") if root is not None else None
    if node is None:
        return "", ""
    url = http_url(node.get("cdnurl"))
    digest = text(node.get("md5"), 64).lower()
    if not url or not re.fullmatch(r"[0-9a-f]{32}", digest):
        return "", ""
    parsed = urlsplit(url)
    # Separate public CDN client; never forward the local Hook token or follow redirects.
    if not (parsed.hostname == "qpic.cn" or parsed.hostname.endswith(".qpic.cn")):
        return "", ""
    try:
        if parsed.port not in {None, 80, 443}:
            return "", ""
    except ValueError:
        return "", ""
    return urlunsplit(("https", parsed.hostname, parsed.path, parsed.query, "")), digest


def forward_nodes(content: str) -> list[dict] | None:
    root = xml_body(content)
    if root is None:
        return None
    app = root if root.tag == "appmsg" else root.find("appmsg")
    if app is None or app.findtext("type") != "19":
        return None
    record = app.find("recorditem")
    if record is None:
        return []
    if record.text and record.text.strip():
        record = xml_body(record.text)
        if record is None:
            return []
    result, remaining, truncated = [], 16000, False

    def append(name, body):
        nonlocal remaining
        body = body[:min(2000, remaining)]
        result.append({"name": name[:160], "text": body})
        remaining -= len(body)

    def expand(record, depth=0, parent=""):
        nonlocal truncated
        items = record.findall("./dataitem") if record.tag == "datalist" else record.findall("./datalist/dataitem")
        if not items:
            items = record.findall("./recordinfo/datalist/dataitem")
        for item in items:
            if len(result) >= 40 or remaining <= 0:
                truncated = True
                break
            name = text(item.findtext("sourcename"), 160) or "未知发送者"
            name = (parent + " / " + name) if parent else name
            kind = item.get("datatype")
            if kind == "17" and depth < 2:
                nested = item.find("recordinfo")
                if nested is None:
                    nested_text = item.findtext("recordxml") or item.findtext("datadesc") or ""
                    nested = xml_body(nested_text)
                previous = len(result)
                if nested is not None:
                    expand(nested, depth + 1, name[:160])
                if len(result) > previous:
                    continue
            if kind == "1":
                body = text(item.findtext("datadesc"), 2000)
            elif kind == "5":
                body = "[链接] " + text(item.findtext("datatitle"), 300) + " " + http_url(item.findtext("weburlitem/link"))
            else:
                label = {"2": "图片", "3": "语音", "4": "视频", "8": "文件", "17": "嵌套转发"}.get(kind, "附件")
                body = f"[{label}：未展开] " + text(item.findtext("datatitle"), 300)
            append(name, body)

    expand(record)
    if truncated:
        result.append({"name": "系统", "text": "[其余转发节点已截断]"})
    return result


def card_metadata(raw: dict) -> dict:
    card = raw.get("card")
    if not isinstance(card, dict):
        return {}
    result = {key: text(card.get(key)) for key in ("title", "description")}
    result["url"] = http_url(card.get("url"))
    mini = card.get("mini_program")
    if isinstance(mini, dict):
        result["mini_program"] = {key: text(mini.get(key), 500) for key in ("app_id", "username", "page_path")}
    return result


def file_name(raw: dict) -> str:
    card = raw.get("card")
    name = text(card.get("title"), 200) if isinstance(card, dict) else ""
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name).strip(" .")
    return name or "attachment"


def quoted_file_card(quote: dict) -> dict:
    """Recognize file subtype 6 only; never treat every appmsg/49 as a file."""
    if str(quote.get("message_type")) != "49":
        return {}
    content = quote.get("content")
    root = xml_body(content) if isinstance(content, str) else None
    app = root if root is not None and root.tag == "appmsg" else root.find("appmsg") if root is not None else None
    if app is None or (app.findtext("type") or "").strip() != "6":
        media = quote.get("media")
        return {"title": "attachment"} if isinstance(media, dict) and media.get("kind") == "file" else {}
    return {"title": file_name({"card": {"title": app.findtext("title") or "attachment"}})}


def location_metadata(content: str) -> dict | None:
    root = xml_body(content)
    node = root if root is not None and root.tag == "location" else root.find("location") if root is not None else None
    if node is None:
        return None
    try:
        lat, lon = float(node.get("x", "")), float(node.get("y", ""))
    except (ValueError, TypeError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {"lat": lat, "lon": lon, "title": text(node.get("poiname"), 300),
            "content": text(node.get("label"), 1000)}


def contact_metadata(raw: dict, content: str) -> dict:
    card = raw.get("card")
    if isinstance(card, dict) and card.get("username"):
        return {key: text(card.get(key), 300) for key in ("username", "nickname", "alias")}
    root = xml_body(content)
    if root is None or root.tag != "msg":
        return {}
    return {key: text(root.get(key), 300) for key in ("username", "nickname", "alias")}


def special_metadata(content: str) -> dict:
    root = xml_body(content)
    if root is None:
        return {}
    # Preserve readable leaf fields as data, not executable card actions or raw XML.
    fields = []
    for node in root.iter():
        if not len(node) and node.text and node.text.strip():
            fields.append({"field": text(node.tag, 100), "value": text(node.text.strip(), 500)})
            if len(fields) >= 24:
                break
    return {"root": text(root.tag, 100), "subtype": text(root.get("type"), 100), "fields": fields}
