"""Translate structured Hook quote metadata; no XML parsing or history fetch."""
from astrbot.api.message_components import Plain, Reply


def quote_text(quote: dict) -> str:
    if str(quote.get("message_type")) == "1":
        return str(quote.get("content") or "")[:4500]
    return {"3": "[引用图片]", "43": "[引用视频]", "34": "[引用语音]"}.get(
        str(quote.get("message_type")), "[引用消息]",
    )


def make_reply(quote: dict, attachment=None) -> Reply | None:
    message_id = quote.get("msgsvrid")
    # Keep native 64-bit message IDs as strings; never synthesize a quote ID.
    if (not isinstance(message_id, str) or not message_id.isdecimal()
            or message_id == "0" or len(message_id) > 20):
        return None
    text = quote_text(quote)
    chain = [Plain(text=text)]
    if attachment is not None:
        chain.append(attachment)
    try:
        timestamp = int(quote.get("message_time") or 0)
    except (ValueError, TypeError, OverflowError):
        timestamp = 0
    return Reply(
        id=message_id, chain=chain, sender_id=str(quote.get("sender_id") or ""),
        sender_nickname=str(quote.get("display_name") or "")[:160],
        time=timestamp, message_str=text,
    )
