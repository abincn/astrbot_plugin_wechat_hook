"""Verify media downloaded from Hook before exposing it to AstrBot."""
import hashlib
import json
import logging
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 12 * 1024 * 1024


def save_verified_image(folder: str | Path, data: bytes, artifact: dict) -> Path:
    size = int(artifact.get("bytes") or 0)
    digest = str(artifact.get("sha256") or "").lower()
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise ValueError("Image size out of bounds")
    if size != len(data) or len(digest) != 64 or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Image SHA256/size mismatch")
    if data.startswith(b"\xff\xd8\xff"):
        ext = ".jpg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = ".png"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        ext = ".gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = ".webp"
    else:
        raise ValueError("Unknown image signature")
    target = Path(folder).resolve() / (digest + ext)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temporary = target.with_suffix(ext + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
    return target


async def process_pending_media(inbox, request, client, media_root: str | Path) -> None:
    """Advance pending Hook media tasks once; never replay an uncertain create."""
    for item in inbox.pending_media():
        event_id = item["event_id"]
        raw = json.loads(item["raw_json"])
        if item["status"] == "new":
            if not inbox.begin_media(event_id):
                continue
            try:
                original = str(raw.get("msgsvrid") or "")
                if not original.isdecimal():
                    raise ValueError("Missing numeric msgsvrid")
                payload = {
                    "msgsvrid": original,
                    "source_talker": item["sender"],
                    "kind": "image",
                    "download": True,
                    "image_quality": "full",
                    "request_id": item["request_id"],
                }
                _, response = await request("POST", "/wxcx/media/tasks", data=payload)
                if not isinstance(response, dict):
                    raise ValueError("Hook media create response has no object")
                task_id = str(response.get("task_id") or "")
                if not task_id:
                    raise RuntimeError("Hook did not issue media task_id")
                inbox.media_pending(event_id, task_id)
            except Exception as exc:
                inbox.media_unknown(event_id, type(exc).__name__)
            continue

        task_id = str(item.get("task_id") or "")
        if not task_id:
            inbox.media_unknown(event_id, "Missing accepted task ID")
            continue
        try:
            _, response = await request("GET", "/wxcx/media/tasks/" + task_id)
            if not isinstance(response, dict):
                raise ValueError("Hook media status response has no object")
            task = response.get("task") or response
            if not isinstance(task, dict):
                raise ValueError("Hook media task has no object")
            state = str(task.get("state") or "").lower()
            if state in {"failed", "error", "cancelled", "stopped"}:
                inbox.media_failed(event_id, "Hook media state: " + state)
            elif state in {"unknown", "result_unknown", "timed_out", "timeout", "account_changed"}:
                inbox.media_unknown(event_id, "Hook media state: " + state)
            elif state == "ready":
                artifact = task.get("artifact") or response.get("artifact") or {}
                if not isinstance(artifact, dict):
                    raise ValueError("Hook media artifact has no object")
                size = int(artifact.get("bytes") or 0)
                if not 0 < size <= MAX_IMAGE_BYTES:
                    raise ValueError("Hook artifact size invalid")
                assert client is not None
                async with client.stream(
                    "GET", "/wxcx/media/tasks/" + task_id + "/content",
                ) as content:
                    content.raise_for_status()
                    buf = bytearray()
                    async for part in content.aiter_bytes():
                        buf.extend(part)
                        if len(buf) > MAX_IMAGE_BYTES:
                            raise ValueError("Image exceeds size limit")
                path = save_verified_image(media_root, bytes(buf), artifact)
                inbox.media_ready(event_id, str(path), artifact)
        except (httpx.HTTPError, RuntimeError):
            # A status request can fail temporarily; keep the accepted task pending.
            log.warning("Media task status/content currently unavailable for %s", event_id)
        except (ValueError, KeyError, TypeError) as exc:
            inbox.media_failed(event_id, str(exc))
