"""Verify media downloaded from Hook before exposing it to AstrBot."""
import hashlib
import json
from pathlib import Path
import re

import httpx
from astrbot.api import logger as log
from .events import normalize_event
from .extra_media import download_voice, download_emoji


MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_VIDEO_BYTES = 60 * 1024 * 1024
LIMITS = {"image": MAX_IMAGE_BYTES, "video": MAX_VIDEO_BYTES, "file": 24 * 1024 * 1024}


def safe_code(value, fallback: str) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value) else fallback


def http_status(exc: Exception):
    status = getattr(exc, "status_code", None)
    if status is None and isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    return status


def request_error(exc: Exception) -> str:
    status = http_status(exc)
    code = safe_code(getattr(exc, "error_code", None), "")
    if status is not None:
        return f"hook_http_{status}" + (":" + code if code else "")
    return type(exc).__name__


def save_verified_media(folder: str | Path, data: bytes, artifact: dict, kind: str) -> Path:
    size = int(artifact.get("bytes") or 0)
    digest = str(artifact.get("sha256") or "").lower()
    if (kind not in LIMITS or not isinstance(data, bytes) or len(data) > LIMITS[kind]
            or (not data and kind != "file")):
        raise ValueError("Media size out of bounds")
    if size != len(data) or len(digest) != 64 or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Media SHA256/size mismatch")
    if kind == "file":
        suffix = str(artifact.get("extension") or "").lower().lstrip(".")
        allowed = {"txt", "md", "csv", "json", "log", "xml", "pdf", "docx", "xlsx", "pptx", "doc", "xls", "ppt"}
        ext = "." + suffix if suffix in allowed else ".bin"
    elif kind == "video" and len(data) > 12 and data[4:8] == b"ftyp":
        ext = ".mp4"
    elif kind == "video" and data.startswith(bytes.fromhex("1a45dfa3")):
        ext = ".webm"
    elif kind == "video":
        raise ValueError("Unsupported video container")
    elif data.startswith(b"\xff\xd8\xff"):
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


def save_verified_image(folder: str | Path, data: bytes, artifact: dict) -> Path:
    return save_verified_media(folder, data, artifact, "image")


def verify_task_source(task: dict, source) -> None:
    expected = {"kind": source.media_kind, "msgsvrid": source.media_msgsvrid,
                "source_talker": source.media_source_talker}
    for key, value in expected.items():
        if key in task and str(task[key]) != value:
            raise ValueError("Hook media source identity mismatch")


async def process_pending_media(inbox, request, client, media_root: str | Path) -> None:
    """Advance pending Hook media tasks once; never replay an uncertain create."""
    for item in inbox.pending_media():
        event_id = item["event_id"]
        try:
            raw = json.loads(item["raw_json"])
            source = normalize_event(raw, item["owner"])
        except (ValueError, TypeError) as exc:
            inbox.media_failed(event_id, "Invalid persisted media source: " + type(exc).__name__)
            continue
        if source is None or source.media_kind not in {*LIMITS, "voice", "emoji"}:
            inbox.media_failed(event_id, "Unsupported media source")
            continue
        if source.service_notice:
            inbox.mark_service_notice(event_id, source.service_notice)
            continue
        kind = source.media_kind
        if kind in {"voice", "emoji"}:
            if item["status"] != "new" or not inbox.begin_media(event_id):
                continue
            try:
                if kind == "voice":
                    path, artifact = await download_voice(client, source, media_root)
                else:
                    path, artifact = await download_emoji(source, media_root, save_verified_image)
                inbox.media_ready(event_id, str(path), artifact)
                log.info(f"Hook {kind} ready for event {event_id}")
            except (httpx.HTTPError, ValueError, TypeError, OSError) as exc:
                reason = request_error(exc)
                inbox.media_unavailable(event_id, f"{kind}_unavailable: {reason}")
                log.warning(f"Hook {kind} unavailable for event {event_id}: {reason}")
            continue
        if item["status"] == "new":
            if not inbox.begin_media(event_id):
                continue
            try:
                log.info(f"Hook {kind} received; requesting media for event {event_id}, source_talker={source.media_source_talker}")
                original = source.media_msgsvrid
                if not original.isdecimal():
                    raise ValueError("Missing numeric msgsvrid")
                payload = {
                    "msgsvrid": original,
                    "source_talker": source.media_source_talker,
                    "kind": kind,
                    "download": True,
                    "request_id": item["request_id"],
                }
                if kind == "image":
                    payload["image_quality"] = "full"
                _, response = await request("POST", "/wxcx/media/tasks", data=payload)
                if not isinstance(response, dict):
                    raise ValueError("Hook media create response has no object")
                task_id = str(response.get("task_id") or "")
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", task_id):
                    raise RuntimeError("Hook did not issue media task_id")
                if isinstance(response.get("task"), dict):
                    verify_task_source(response["task"], source)
                inbox.media_pending(event_id, task_id)
                log.info(f"Hook {kind} task accepted: {task_id}, event={event_id}")
            except Exception as exc:
                reason = request_error(exc)
                inbox.media_unknown(event_id, reason)
                log.warning(f"Hook {kind} media request unresolved for event {event_id}: {reason}; no automatic resubmission")
            continue

        task_id = str(item.get("task_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", task_id):
            inbox.media_unknown(event_id, "Missing accepted task ID")
            continue
        try:
            _, response = await request("GET", "/wxcx/media/tasks/" + task_id)
            if not isinstance(response, dict):
                raise ValueError("Hook media status response has no object")
            task = response.get("task") or response
            if not isinstance(task, dict):
                raise ValueError("Hook media task has no object")
            verify_task_source(task, source)
            state = str(task.get("state") or "").lower()
            if state in {"failed", "error", "cancelled", "stopped"}:
                reason = safe_code(task.get("error"), "hook_media_" + state)
                inbox.media_unavailable(event_id, reason)
                log.warning(f"Hook {kind} failed: event={event_id}, task={task_id}, error={reason}")
            elif state in {"unknown", "result_unknown", "timed_out", "timeout", "account_changed"}:
                reason = safe_code(task.get("error"), "hook_media_" + state)
                inbox.media_unknown(event_id, reason)
                log.warning(f"Hook {kind} unresolved: event={event_id}, task={task_id}, error={reason}; no automatic resubmission")
            elif state == "ready":
                artifact = task.get("artifact") or response.get("artifact") or {}
                if not isinstance(artifact, dict):
                    raise ValueError("Hook media artifact has no object")
                size = int(artifact.get("bytes") or 0)
                if not (0 <= size <= LIMITS[kind]) or (size == 0 and kind != "file"):
                    raise ValueError("Hook artifact size invalid")
                assert client is not None
                async with client.stream(
                    "GET", "/wxcx/media/tasks/" + task_id + "/content",
                ) as content:
                    content.raise_for_status()
                    buf = bytearray()
                    async for part in content.aiter_bytes():
                        buf.extend(part)
                        if len(buf) > LIMITS[kind]:
                            raise ValueError("Media exceeds size limit")
                path = save_verified_media(media_root, bytes(buf), artifact, kind)
                inbox.media_ready(event_id, str(path), artifact)
                log.info(f"Hook {kind} ready for event {event_id}, task={task_id}")
        except (httpx.HTTPError, RuntimeError) as exc:
            # A status request can fail temporarily; keep the accepted task pending.
            reason = request_error(exc)
            if http_status(exc) in {404, 410}:
                inbox.media_unknown(event_id, reason)
            log.warning(f"Hook {kind} task status/content unavailable: event={event_id}, task={task_id}, error={reason}")
        except (ValueError, KeyError, TypeError, OSError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            inbox.media_unavailable(event_id, reason)
            log.warning(f"Hook {kind} verification failed for event {event_id}, task={task_id}: {reason}")
