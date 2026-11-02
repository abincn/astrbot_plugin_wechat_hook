"""One native text submission per durable intent; never promote old drafts."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid


log = logging.getLogger(__name__)
TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,160}")
STATES = {"requesting": "send_requesting", "queued": "send_queued",
          "submitted_unverified": "submitted_unverified",
          "unknown": "send_unknown", "failed": "send_failed"}


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
                ("Native text submission requires review: " + error, record_id),
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
            request_id = "astrbot-text-" + uuid.uuid4().hex
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
        request_id = self._reserve(record_id, payload)
        if request_id is None:
            return
        try:
            _, response = await self.request(
                "POST", "/SendAtMsg_NoSrc" if mentions else "/SendTextMsg",
                data={**payload, "request_id": request_id},
            )
            if (not isinstance(response, dict) or str(response.get("code")) != "0"
                    or not (response.get("accepted") is True or response.get("replayed") is True)
                    or not isinstance(response.get("task_id"), str)
                    or not TASK_ID.fullmatch(response["task_id"])):
                raise ValueError("Unrecognized Hook send acknowledgement")
            task_id = response["task_id"]
        except asyncio.CancelledError:
            with self.inbox._transaction():
                self._update(record_id, "unknown", error="cancelled_before_ack")
            raise
        except Exception as exc:
            code = getattr(exc, "status_code", None)
            state = "failed" if code in {400, 401, 403, 404, 409, 413, 422, 501} else "unknown"
            with self.inbox._transaction():
                self._update(record_id, state, error=f"hook_http_{code}" if code else type(exc).__name__)
            raise
        with self.inbox._transaction():
            self._update(record_id, "queued", task_id)
        log.info("Hook accepted text task %s; recipient delivery is unverified", task_id)

    async def poll_once(self):
        with self.inbox._lock:
            rows = self.inbox.conn.execute(
                "SELECT s.outbound_id,s.task_id FROM outbound_send_attempts s "
                "JOIN outbound o ON o.id=s.outbound_id WHERE o.owner=? AND s.status='queued' "
                "ORDER BY s.updated_at LIMIT 32", (self.owner,),
            ).fetchall()
        for row in rows:
            try:
                _, response = await self.request("GET", "/wxcx/text/tasks/" + row["task_id"])
                task = response.get("task") if isinstance(response, dict) else None
                if not isinstance(task, dict) or task.get("task_id") != row["task_id"]:
                    raise ValueError("Unrecognized Hook task status")
                status = task.get("state")
                if status in {"queued", "running", "submitting"}:
                    continue
                if status == "submitted" and task.get("local_submission_complete") is True:
                    status = "submitted_unverified"
                elif status != "failed":
                    status = "unknown"
                error = "" if status == "submitted_unverified" else "hook_task_" + status
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if getattr(exc, "status_code", None) == 404 or isinstance(exc, ValueError):
                    status, error = "unknown", "hook_task_missing_or_invalid"
                else:
                    log.warning("Hook text status unavailable (%s)", type(exc).__name__)
                    continue
            with self.inbox._transaction():
                self._update(row["outbound_id"], status, row["task_id"], error)
            log.info("Hook text task %s: %s", row["task_id"], status)
