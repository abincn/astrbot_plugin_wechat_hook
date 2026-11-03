"""Map inbound content to AstrBot components, without network or model calls."""
import re

from astrbot.api.message_components import At, AtAll, File, Image, Json, Location, Node, Nodes, Plain, Record, Share, Video

from .payloads import card_metadata, contact_metadata, file_name, forward_nodes, location_metadata, special_metadata, text
from .quotes import make_reply, quote_text
from .payloads import quoted_file_card


def mention_components(item) -> list:
    if not item.is_group:
        return []
    values = item.raw.get("mentions")
    values = values[:1024] if isinstance(values, list) else []
    chain, seen = [], set()
    for value in values:
        if not isinstance(value, str) or value in seen:
            continue
        if value == "notify@all":
            chain.append(AtAll())
        elif re.fullmatch(r"[A-Za-z0-9_.@-]{1,256}", value) and not value.endswith("@chatroom"):
            chain.append(At(qq=value))
        else:
            continue
        seen.add(value)
    if item.at_me is True and item.owner not in seen:
        chain.append(At(qq=item.owner))
    return chain


def inbound_content(item) -> tuple[str, list]:
    content = "" if item.raw.get("content_complete") is False else text(item.raw.get("content"), 96000)
    attachment = None
    if item.media_path:
        if item.media_kind == "file":
            quoted = quoted_file_card(item.quote) if item.quote else {}
            attachment = File(name=quoted.get("title") or file_name(item.raw), file=item.media_path)
        else:
            component = {"image": Image, "emoji": Image, "video": Video, "voice": Record}[item.media_kind]
            attachment = component.fromFileSystem(item.media_path)
    quote = item.quote
    if quote is not None:
        body = text(quote.get("reply_text"), 4500) or "[引用回复]"
        missing = ""
        if attachment is None and (item.media_kind or str(quote.get("message_type")) in {"3", "43", "62"}):
            missing = "[引用附件未取得，无法读取内容" + ("；" + item.media_error if item.media_error else "") + "]"
            body += "\n" + missing
        reply = make_reply(quote, attachment)
        if reply is not None and missing:
            reply.chain.append(Plain(text=missing))
        chain = [reply] if reply else [Plain(text="[引用原文；缺少有效原消息 ID]\n" + quote_text(quote))]
        chain.append(Plain(text=body))
        if attachment is not None and reply is None:
            chain.append(attachment)
        return body, chain
    labels = {"image": "图片", "video": "视频", "voice": "语音", "emoji": "表情", "file": "文件"}
    if item.media_kind:
        body = "[" + labels[item.media_kind] + "]"
        if item.media_kind == "file":
            body += " " + file_name(item.raw)
        if attachment is None:
            body += " [附件未取得，无法读取内容" + ("；" + item.media_error if item.media_error else "") + "]"
        # Keep message_str as the model caption; the component already renders
        # its media label in AstrBot logs, so don't add a duplicate Plain label.
        return body, [attachment] if attachment is not None else [Plain(text=body)]
    if item.kind == 42:
        card = contact_metadata(item.raw, content)
        body = "[微信名片]\n" + ("\n".join(f"{key}: {value}" for key, value in card.items() if value) or "[名片字段不可读取]")
        # AstrBot Contact.id is an integer/TODO; a WeChat username must stay a string.
        return body, [Plain(text=body), Json(data={"kind": "wechat_contact", **card})]
    if item.kind == 48:
        location = location_metadata(content)
        if location is None:
            body = "[位置：缺少完整、有效的坐标，无法读取]"
            return body, [Plain(text=body)]
        body = f"[位置] {location['title']}\n{location['content']}\n纬度 {location['lat']}，经度 {location['lon']}"
        return body, [Plain(text=body), Location(**location)]
    if item.kind == 49:
        metadata = item.raw.get("source_meta")
        metadata = metadata if isinstance(metadata, dict) else {}
        kind = item.raw.get("message_kind") or metadata.get("message_kind")
        card = card_metadata(item.raw)
        if kind == "file_transfer_notice":
            transfer = item.raw.get("file_transfer")
            transfer = transfer if isinstance(transfer, dict) else {}
            info = {key: transfer.get(key) for key in ("phase", "status_code", "replaces_msgsvrid")
                    if isinstance(transfer.get(key), (str, int))}
            body = "[文件传输通知] " + file_name(item.raw) + "\n" + str(info)
            return body, [Plain(text=body), Json(data={"kind": "wechat_file_transfer", **info})]
        if kind in {"link_card", "mini_program_card"}:
            label = "小程序" if kind == "mini_program_card" else "链接"
            body = f"[{label}] " + card.get("title", "")
            body += "\n" + card.get("description", "")
            if card.get("url"):
                body += "\n" + card["url"]
            extra = []
            if kind == "link_card" and card.get("url"):
                extra.append(Share(url=card["url"], title=card.get("title") or "链接", content=card.get("description", "")))
            mini = card.get("mini_program")
            if mini:
                body += "\n" + "\n".join(f"{key}: {value}" for key, value in mini.items() if value)
                extra.append(Json(data={"kind": "wechat_mini_program", **card}))
            return body.strip(), [Plain(text=body.strip()), *extra]
        nodes = forward_nodes(content)
        if nodes is not None:
            body = "[合并转发；仅展开内嵌文字，附件未下载]"
            body += "\n" + ("\n".join(node["name"] + ": " + node["text"] for node in nodes) or "[没有可读取的内嵌节点]")
            # Plain summary is also needed by AstrBot versions that don't expand Nodes for the model.
            chain = [Plain(text=body)]
            if nodes:
                chain.append(Nodes(nodes=[Node(name=node["name"], content=[Plain(text=node["text"])]) for node in nodes]))
            return body, chain
        body = "[应用消息：仅接收内嵌元数据，未执行卡片操作]"
        if card.get("title"):
            body += " " + card["title"]
        metadata = special_metadata(content)
        details = "\n".join(field["field"] + ": " + field["value"] for field in metadata.get("fields", []))
        if details:
            body += "\n" + details
        return body, [Plain(text=body), Json(data={"kind": "wechat_app_message", **metadata})]
    if item.kind == 1:
        return item.content, [Plain(text=item.content)]
    # Keep special messages visible, with bounded leaf metadata and raw_message for other plugins.
    label = "系统通知" if item.kind in {10000, 10002} else f"微信特殊消息 type={item.kind}"
    metadata = special_metadata(content)
    details = "\n".join(field["field"] + ": " + field["value"] for field in metadata.get("fields", []))
    if not metadata and content and not content.lstrip().startswith("<"):
        details = content[:2000]
    body = f"[{label}；未执行专用操作]" + ("\n" + details if details else "")
    return body, [Plain(text=body), Json(data={"kind": "wechat_special", "type": item.kind, **metadata})]
