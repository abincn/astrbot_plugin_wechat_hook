"""One native submission per durable intent; never promote old drafts."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from astrbot.api import logger as log


TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,160}")
STATES = {"requesting": "send_requesting", "queued": "send_queued",
          "submitted_unverified": "submitted_unverified",
          "unknown": "send_unknown", "failed": "send_failed"}
IMAGE_BYTES_LIMIT = 4 * 1024 * 1024  # Hook fileData limit before hex encoding.


def read_reply_image(location: str | None) -> tuple[bytes, str]:
    path = Path(location or "")
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Outgoing image must be an existing local file on AstrBot")
    with path.open("rb") as source:
        data = source.read(IMAGE_BYTES_LIMIT + 1)
    if len(data) > IMAGE_BYTES_LIMIT:
        raise ValueError("Outgoing image exceeds the Hook's 4 MiB byte upload limit")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data, "astrbot-image.png"
    if data.startswith(b"\xff\xd8\xff"):
        return data, "astrbot-image.jpg"
    raise ValueError("Outgoing image must be PNG or JPEG")


async def prepare_reply_image(image) -> tuple[bytes, str]:
    """Resolve through AstrBot and release only resolver-owned temporary files.

    Hook uploads bytes, so the resolved path need only live during this read.
    Unlike component.to_path(), as_path() retains ownership information and
    never deletes a caller's local file or a persistent Inbox attachment.
    """
    from astrbot.core.utils.media_utils import MediaResolver

    async with MediaResolver(image.url or image.file, media_type="image").as_path() as resolved:
        return await asyncio.to_thread(read_reply_image, str(resolved.path))


@asynccontextmanager
async def prepare_reply_file(component, *, voice: bool = False):
    from astrbot.core.utils.media_utils import MediaResolver

    reference = (component.url or component.path or component.file) if voice else (component.url or component.file_)
    async with MediaResolver(reference, media_type="audio" if voice else "file").as_path() as resolved:
        path = Path(resolved.path)
        name = "astrbot-voice.wav" if voice else (component.name or path.name).replace("\\", "/").rsplit("/", 1)[-1]
        def digest():
            sha = hashlib.sha256()
            with path.open("rb") as source:
                while chunk := source.read(65536):
                    sha.update(chunk)
            return sha.hexdigest()
        yield path, name, await asyncio.to_thread(digest)


class NativeTextSender:
    def __init__(self, inbox, request, owner: str):
        self.inbox, self.request, self.owner = inbox, request, owner
        with inbox._transaction():
            inbox.conn.execute("""
                CREATE TABLE IF NOT EXISTS outbound_send_attempts (
                    outbound_id INTEGER PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    request_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL
                )
            """)
            interrupted = inbox.conn.execute(
                "SELECT s.outbound_id FROM outbound_send_attempts s "
                "JOIN outbound o ON o.id=s.outbound_id "
                "WHERE o.owner=? AND s.status='requesting'", (owner,),
            ).fetchall()
            for row in interrupted:
                self._update(row["outbound_id"], "unknown", error="interrupted_before_ack")
            recent = inbox.conn.execute(
                "SELECT s.status,s.task_id,s.error,s.payload_json FROM outbound_send_attempts s "
                "JOIN outbound o ON o.id=s.outbound_id WHERE o.owner=? "
                "ORDER BY s.updated_at DESC,s.outbound_id DESC LIMIT 5", (owner,),
            ).fetchall()
        for row in recent:
            kind = json.loads(row["payload_json"]).get("kind", "text")
            log.info("Recovered Hook %s task %s: %s; error=%s",
                     kind, row["task_id"], row["status"], row["error"] or "none")

    def _update(self, record_id: int, status: str, task_id: str = "", error: str = ""):
        self.inbox.conn.execute(
            "UPDATE outbound_send_attempts SET status=?,task_id=?,error=?,updated_at=? "
            "WHERE outbound_id=?", (status, task_id, error, int(time.time()), record_id),
        )
        self.inbox.conn.execute(
            "UPDATE outbound SET status=?,task_id=?,error=? WHERE id=?",
            (STATES[status], task_id, error, record_id),
        )
        if status in {"unknown", "failed"}:
            self.inbox.conn.execute(
                "UPDATE inbox SET status='native_send_review',last_error=? "
                "WHERE event_id=(SELECT source_event_id FROM outbound WHERE id=?) "
                "AND status IN ('dispatched','processed')",
                ("Native submission requires review: " + error, record_id),
            )

    def _reserve(self, record_id: int, payload: dict) -> str | None:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.inbox._transaction():
            old = self.inbox.conn.execute(
                "SELECT * FROM outbound_send_attempts WHERE outbound_id=?", (record_id,),
            ).fetchone()
            if old:
                if old["payload_json"] != serialized:
                    raise ValueError("Native send intent payload conflict")
                if old["status"] in {"queued", "submitted_unverified"}:
                    return None
                raise RuntimeError("Native send already attempted; manual review required")
            row = self.inbox.conn.execute(
                "SELECT owner,room,content,status FROM outbound WHERE id=?", (record_id,),
            ).fetchone()
            if (row is None or row["owner"] != self.owner
                    or row["room"] != payload["toid"] or row["content"] != payload["msg"]
                    or row["status"] != "send_prepared"):
                raise ValueError("Only a new explicitly enabled reply may be submitted")
            request_id = "astrbot-" + payload.get("kind", "text") + "-" + uuid.uuid4().hex
            self.inbox.conn.execute(
                "INSERT INTO outbound_send_attempts"
                "(outbound_id,payload_json,request_id,status,updated_at) VALUES(?,?,?,'requesting',?)",
                (record_id, serialized, request_id, int(time.time())),
            )
            self.inbox.conn.execute(
                "UPDATE outbound SET status='send_requesting' WHERE id=?", (record_id,),
            )
            return request_id

    async def send_once(self, record_id: int, room: str, text: str, mentions: list[str]):
        payload = {"toid": room, "msg": text}
        if mentions:
            payload["atlist"] = mentions
        await self._submit(record_id, payload, "/SendAtMsg_NoSrc" if mentions else "/SendTextMsg", payload)

    async def send_image_once(self, record_id: int, room: str, data: bytes, name: str):
        # Persist the intent and digest, not private image bytes or container paths.
        wire = {"kind": "image", "toid": room, "fileName": name}
        intent = {**wire, "msg": "[Image]", "sha256": hashlib.sha256(data).hexdigest()}
        await self._submit(record_id, intent, "/wxcx/outbound/tasks", {**wire, "fileData": data.hex()})

    async def send_attachment_once(self, record_id: int, room: str, path: Path, name: str, digest: str, *, kind: str):
        wire = {"kind": kind, "toid": room, "fileName": name}
        intent = {**wire, "msg": "[Voice]" if kind == "voice" else "[File]", "sha256": digest}
        await self._submit(record_id, intent, "/wxcx/outbound/upload", wire, upload=path)
        if kind == "voice":
            # The music caller may fall back to File on a definite rejection.
            # Queue acceptance alone cannot decide that. Read the durable result
            # written by poll_once(), which also owns native task release.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 300  # Existing Hook task wait, not audio duration.
            while True:
                with self.inbox._lock:
                    result = self.inbox.conn.execute(
                        "SELECT status,task_id,error FROM outbound_send_attempts WHERE outbound_id=?",
                        (record_id,),
                    ).fetchone()
                if result["status"] == "submitted_unverified":
                    return
                if result["status"] == "failed":
                    raise RuntimeError(f"Hook voice task {result['task_id']} failed: {result['error']}")
                if result["status"] == "unknown" or loop.time() >= deadline:
                    log.warning(
                        "Hook voice task %s completion unconfirmed: %s; no automatic file fallback",
                        result["task_id"], result["error"] or "native_task_wait_elapsed",
                    )
                    return
                await asyncio.sleep(0.5)

    async def _submit(self, record_id: int, intent: dict, route: str, wire: dict, *, upload: Path | None = None):
        request_id = self._reserve(record_id, intent)
        if request_id is None:
            return
        kind = intent.get("kind", "text")
        try:
            options = {"data": {**wire, "request_id": request_id}}
            if upload is not None:
                options["upload"] = upload
            http_status, response = await self.request("POST", route, **options)
            if not isinstance(response, dict):
                raise ValueError("Unrecognized Hook send acknowledgement")
            task_id = response.get("task_id")
            if kind in {"image", "file", "voice"}:
                accepted = (http_status in (200, 202) and response.get("kind") == kind
                            and response.get("toid") == intent["toid"])
            else:
                accepted = (str(response.get("code")) == "0"
                            and (response.get("accepted") is True or response.get("replayed") is True))
            if not accepted or not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
                raise ValueError("Unrecognized Hook send acknowledgement")
        except asyncio.CancelledError:
            with self.inbox._transaction():
                self._update(record_id, "unknown", error="cancelled_before_ack")
            raise
        except Exception as exc:
            code = getattr(exc, "status_code", None)
            state = "failed" if code in {400, 401, 403, 404, 409, 410, 413, 415, 422, 429, 501} else "unknown"
            error = f"hook_http_{code}" if code else str(exc) or type(exc).__name__
            if getattr(exc, "error_code", ""):
                error += ":" + exc.error_code
            with self.inbox._transaction():
                self._update(record_id, state, error=error[:250])
            log.warning("Hook %s submission %s: %s", kind, state, error[:250])
            raise
        with self.inbox._transaction():
            self._update(record_id, "queued", task_id)
        log.info("Hook accepted %s task %s; recipient delivery is unverified", kind, task_id)

    async def poll_once(self):
        with self.inbox._lock:
            rows = self.inbox.conn.execute(
                "SELECT s.outbound_id,s.task_id,s.payload_json FROM outbound_send_attempts s "
                "JOIN outbound o ON o.id=s.outbound_id WHERE o.owner=? AND s.status='queued' "
                "ORDER BY s.updated_at LIMIT 32", (self.owner,),
            ).fetchall()
        for row in rows:
            kind = json.loads(row["payload_json"]).get("kind", "text")
            task_id = row["task_id"]
            media = kind in {"image", "file", "voice"}
            route = ("/wxcx/outbound/tasks/" if media else "/wxcx/text/tasks/") + task_id
            release = False
            try:
                _, response = await self.request("GET", route)
                task = response if media else response.get("task") if isinstance(response, dict) else None
                if not isinstance(task, dict) or task.get("task_id") != task_id:
                    raise ValueError("Unrecognized Hook task status")
                status = task.get("state")
                if status in {"queued", "running", "preparing", "prepared", "submitting"}:
                    continue
                if media and task.get("native_pending") is True:
                    continue
                release = (media and task.get("native_pending") is False
                           and status in {"submitted", "failed", "timed_out", "stopped", "result_unknown"})
                if status == "submitted" and task.get(
                    "submission_started" if media else "local_submission_complete"
                ) is True:
                    status = "submitted_unverified"
                elif status in {"timed_out", "stopped"}:
                    status = "failed"
                elif status != "failed":
                    status = "unknown"
                error = "" if status == "submitted_unverified" else str(task.get("error") or "hook_task_" + status)[:250]
                if media and error:
                    error += f"; native_code={task.get('native_code')}; native_detail={task.get('native_detail')}"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if getattr(exc, "status_code", None) == 404 or isinstance(exc, ValueError):
                    status, error = "unknown", "hook_task_missing_or_invalid"
                else:
                    log.warning("Hook %s status unavailable (%s)", kind, type(exc).__name__)
                    continue
            with self.inbox._transaction():
                self._update(row["outbound_id"], status, task_id, error)
            log.info("Hook %s task %s: %s; error=%s", kind, task_id, status, error or "none")
            if release:
                try:
                    await self.request("DELETE", route)
                except Exception as exc:
                    # Result is already durable; a release failure must not trigger another send.
                    log.warning("Hook %s task %s release failed: %s", kind, task_id, exc)
