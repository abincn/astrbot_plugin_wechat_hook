"""Read local Hook voice data and verified public emoji images; no model calls."""
import base64
import hashlib
import json
from pathlib import Path

import httpx

from .payloads import emoji_source, text

MAX_VOICE_BYTES = 4 * 1024 * 1024


async def read_bounded(response, limit: int) -> bytes:
    response.raise_for_status()
    result = bytearray()
    async for chunk in response.aiter_bytes():
        if len(result) + len(chunk) > limit:
            raise ValueError("Media response exceeds size limit")
        result.extend(chunk)
    return bytes(result)


def validate_silk(data: bytes) -> None:
    body = data[1:] if data.startswith((b"\x02#!SILK_V3", b"\x03#!SILK_V3")) else data
    if not body.startswith(b"#!SILK_V3") or not 9 < len(body) <= MAX_VOICE_BYTES:
        raise ValueError("Invalid SILK header or size")
    offset, frames = 9, 0
    while offset + 2 <= len(body):
        size = int.from_bytes(body[offset:offset + 2], "little", signed=True)
        offset += 2
        if size == -1 and offset == len(body):
            break
        if size < 0 or offset + size > len(body):
            raise ValueError("Invalid SILK frame")
        offset += size
        frames += 1
        if frames > 6000:
            raise ValueError("Voice exceeds 120 seconds of SILK frames")
    if offset != len(body) or not frames:
        raise ValueError("Incomplete SILK payload")


async def download_voice(client, source, media_root: str | Path) -> tuple[Path, dict]:
    message_id = source.media_msgsvrid
    if not message_id.isascii() or not message_id.isdecimal() or not 0 < int(message_id) < 2**64:
        raise ValueError("Voice needs a valid native msgsvrid")
    async with client.stream("POST", "/DownloadVoice", json={"msgsvrid": int(message_id), "format": "base64"}) as response:
        raw = await read_bounded(response, 6 * 1024 * 1024)
    result = json.loads(raw)
    if (not isinstance(result, dict) or str(result.get("code")) != "0"
            or str(result.get("msgid")) != message_id):
        raise ValueError("Hook voice is unavailable or source ID differs")
    encoded = result.get("voice_base64")
    if not isinstance(encoded, str) or len(encoded) > (MAX_VOICE_BYTES + 2) // 3 * 4:
        raise ValueError("Voice data missing or oversized")
    data = base64.b64decode(encoded, validate=True)
    if result.get("len") != len(data):
        raise ValueError("Hook voice length mismatch")
    validate_silk(data)
    digest = hashlib.sha256(data).hexdigest()
    folder = Path(media_root).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (digest + ".silk")
    # Publish the downloaded bytes, never a filesystem path returned by Hook.
    temporary = path.with_suffix(".silk.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    return path, {"bytes": len(data), "sha256": digest, "format": "silk", "source": "DownloadVoice"}


async def download_emoji(source, media_root, save_image) -> tuple[Path, dict]:
    content = "" if source.raw.get("content_complete") is False else text(source.raw.get("content"), 96000)
    url, expected_md5 = emoji_source(content)
    if not url:
        raise ValueError("Emoji has no supported CDN URL and MD5")
    async with httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False) as public_client:
        async with public_client.stream("GET", url) as response:
            data = await read_bounded(response, 12 * 1024 * 1024)
    if hashlib.md5(data).hexdigest() != expected_md5:
        raise ValueError("Emoji MD5 mismatch")
    artifact = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "format": "emoji"}
    path = save_image(media_root, data, artifact)
    return path, artifact
