"""SQLite-backed inbound, media and immutable outbound decision records."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .events import Inbound, normalize_event
from .identity import conversation_session, group_session, stable_hash

class DurableInbox:
    """A single adapter instance owns a database and Hook cursor."""
    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.conn.execute("COMMIT")
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise

    def __init__(self, location: str | Path):
        self.location = Path(location).expanduser().resolve()
        self.location.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self.conn = sqlite3.connect(str(self.location), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS inbox (
                event_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL,
                owner TEXT NOT NULL, room TEXT NOT NULL, sender TEXT NOT NULL,
                content TEXT NOT NULL, kind INTEGER NOT NULL, at_me INTEGER,
                created_at INTEGER NOT NULL, raw_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT NOT NULL DEFAULT '',
                inserted_at INTEGER NOT NULL,
                media_kind TEXT NOT NULL DEFAULT '',
                media_path TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS inbox_state ON inbox(status, inserted_at);
            CREATE TABLE IF NOT EXISTS cursor (
                name TEXT PRIMARY KEY, value INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbound (
                id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
                room TEXT NOT NULL, content TEXT NOT NULL,
                status TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media_jobs (
                event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                task_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '', artifact_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL
            );
        """)
        self.conn.execute("CREATE TABLE IF NOT EXISTS workspaces ("
                          "owner TEXT NOT NULL,room TEXT NOT NULL,workspace_id TEXT NOT NULL,"
                          "PRIMARY KEY(owner,room),UNIQUE(workspace_id))")
        # Upgrade databases created by earlier pilot builds in place.
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(inbox)")}
        if "media_kind" not in cols:
            self.conn.execute("ALTER TABLE inbox ADD COLUMN media_kind TEXT NOT NULL DEFAULT ''")
        if "media_path" not in cols:
            self.conn.execute("ALTER TABLE inbox ADD COLUMN media_path TEXT NOT NULL DEFAULT ''")
        media_cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(media_jobs)")}
        for column in ("retry_after", "capacity_retries", "release_after"):
            if column not in media_cols:
                self.conn.execute(f"ALTER TABLE media_jobs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        outbound_cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(outbound)")}
        if "intent_id" not in outbound_cols:
            self.conn.execute("ALTER TABLE outbound ADD COLUMN intent_id TEXT NOT NULL DEFAULT ''")
        if "source_event_id" not in outbound_cols:
            self.conn.execute("ALTER TABLE outbound ADD COLUMN source_event_id TEXT NOT NULL DEFAULT ''")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS outbound_intent_unique "
                          "ON outbound(intent_id) WHERE intent_id <> ''")
        # Preserve attempted changes to immutable reply intents for later review.
        # Deduplicate repeated conflicting replays without touching the original draft.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS outbound_conflicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_id TEXT NOT NULL,
                existing_outbound_id INTEGER NOT NULL,
                requested_owner TEXT NOT NULL,
                requested_room TEXT NOT NULL,
                requested_source_event_id TEXT NOT NULL,
                requested_content TEXT NOT NULL,
                detected_at INTEGER NOT NULL,
                attempt_hash TEXT NOT NULL UNIQUE
            )
        """)

    def reconcile_interrupted_media(self) -> int:
        """Crash during a native media create leaves its result unknown; never replay."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE media_jobs SET status='unknown',error='interrupted_before_task_ack',updated_at=? "
                "WHERE status='requesting'", (int(time.time()),))
            return cur.rowcount

    def reconcile_interrupted_pipeline(self) -> int:
        with self._lock:
            cur = self.conn.execute("UPDATE inbox SET status='interrupted_review', "
                                    "last_error='No pipeline completion before restart' "
                                    "WHERE status='dispatched'")
            return cur.rowcount

    def mark_pipeline_result(self, event_id: str, succeeded: bool, error: str = "") -> None:
        """Compatibility wrapper for existing pipeline success/failure callers."""
        self.mark_pipeline_outcome(event_id, "success" if succeeded else "error", error)

    def mark_pipeline_outcome(self, event_id: str, outcome: str, error: str = "") -> None:
        """Store the real completion class; cancellation is NOT model failure.

        A successfully returned pipeline can still have an outbound conflict:
        AstrBot RespondStage catches send exceptions and continues. In that
        case, never mark the source message as an ordinary processed event.
        """
        states = {
            "success": "processed",
            "error": "pipeline_failed",
            "cancelled": "cancelled_review",
            "agent_no_response": "agent_no_response_review",
        }
        state = states.get(outcome, "pipeline_unknown_review")
        detail = "" if outcome == "success" else (error or "pipeline_status_" + outcome)[:250]
        with self._lock:
            if outcome in {"success", "agent_no_response"}:
                conflicting = self.conn.execute(
                    "SELECT 1 FROM outbound_conflicts WHERE requested_source_event_id=? LIMIT 1",
                    (event_id,),
                ).fetchone() is not None
                if conflicting:
                    state = "draft_conflict_review"
                    detail = "Outbound draft intent conflict requires review"
            self.conn.execute(
                "UPDATE inbox SET status=?,last_error=? "
                "WHERE event_id=? AND status='dispatched'",
                (state, detail, event_id),
            )

    def workspace_id(self, owner: str, room: str) -> str:
        group_session(owner, room)
        name = stable_hash([owner, room])[:32]
        with self._lock:
            self.conn.execute("INSERT OR IGNORE INTO workspaces(owner,room,workspace_id) "
                              "VALUES(?,?,?)", (owner,room,name))
        return name

    def after(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT value FROM cursor WHERE name='hook_after'").fetchone()
            return int(row["value"]) if row else 0

    def checkpoint(self, sequence: int) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO cursor(name,value) VALUES('hook_after',?) "
                              "ON CONFLICT(name) DO UPDATE SET value=excluded.value", (sequence,))

    def reset_cursor(self) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO cursor(name,value) VALUES('hook_after',0) "
                              "ON CONFLICT(name) DO UPDATE SET value=0")

    def ingest_batch(self, events: list[dict], owner: str) -> tuple[int, int]:
        """Insert all accepted events and advance the Hook cursor atomically."""
        inserted = 0
        with self._transaction():
            high = self.after()
            for item in events:
                if not isinstance(item, dict):
                    raise ValueError("Non-object event")
                seq = item.get("sequence", item.get("seq", 0))
                if type(seq) not in (int, str) or not str(seq).isdigit():
                    raise ValueError("Hook event sequence missing or invalid")
                seq = int(seq)
                if seq <= 0:
                    raise ValueError("Hook event sequence missing")
                message = normalize_event(item, owner)
                if message is not None:
                    raw = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                    if len(raw.encode("utf-8")) > 96000:
                        raise ValueError("Hook event metadata exceeds 96KB (cursor not advanced)")
                    text_ready = not message.media_kind
                    state = "pending" if text_ready else "held_media"
                    notice_reason = message.service_notice
                    if notice_reason:
                        state = "service_notice"
                    res = self.conn.execute("""
                        INSERT OR IGNORE INTO inbox
                        (event_id,sequence,owner,room,sender,content,kind,at_me,
                         created_at,raw_json,status,inserted_at,media_kind,media_path)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (message.event_id, message.sequence, message.owner,
                          message.room, message.sender, message.content, message.kind,
                          None if message.at_me is None else int(message.at_me),
                          message.created_at, raw, state, int(time.time()),
                          message.media_kind, ""))
                    if res.rowcount > 0:
                        inserted += 1
                        if notice_reason:
                            self.conn.execute("UPDATE inbox SET last_error=? WHERE event_id=?",
                                              ("service_notice:" + notice_reason, message.event_id))
                        elif (message.media_kind == "emoji" or
                                message.media_kind in {"image", "video", "file", "voice"}
                                and message.media_msgsvrid.isdecimal()):
                            request_id = "astrbot-media-" + stable_hash([message.event_id])[:32]
                            self.conn.execute("""
                                INSERT OR IGNORE INTO media_jobs(event_id,request_id,status,updated_at)
                                VALUES(?,?,?,?)
                            """, (message.event_id, request_id, "new", int(time.time())))
                        elif message.media_kind in {"image", "video", "file", "voice"}:
                            self.conn.execute(
                                "UPDATE inbox SET last_error='missing_media_msgsvrid' WHERE event_id=?",
                                (message.event_id,),
                            )
                            self.conn.execute("UPDATE inbox SET status='pending' WHERE event_id=?", (message.event_id,))
                high = max(high, seq)
            self.conn.execute("INSERT INTO cursor(name,value) VALUES('hook_after',?) "
                              "ON CONFLICT(name) DO UPDATE SET value=excluded.value", (high,))
            return inserted, high

    def pending(self, limit: int = 64) -> list[Inbound]:
        with self._lock:
            rows = self.conn.execute("""
                SELECT i.*
                FROM inbox i
                WHERE i.status='pending' ORDER BY i.sequence, i.inserted_at LIMIT ?
            """, (limit,)).fetchall()
        result = []
        for row in rows:
            message = self._row_to_inbound(row)
            if message.service_notice:
                self.mark_service_notice(message.event_id, message.service_notice)
            else:
                result.append(message)
        return result

    def mark_service_notice(self, event_id: str, reason: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE inbox SET status='service_notice',last_error=? "
                              "WHERE event_id=? AND status IN ('pending','held_media')",
                              ("service_notice:" + reason, event_id))

    @staticmethod
    def _row_to_inbound(r: sqlite3.Row) -> Inbound:
        raw = json.loads(r["raw_json"])
        msg = normalize_event(raw, r["owner"])
        # Persisted rows are already validated; normalize once more to recover source metadata.
        return Inbound(r["event_id"], r["sequence"], r["owner"], r["room"], r["sender"],
                       msg.sender_name if msg else r["sender"], r["content"], r["kind"],
                       None if r["at_me"] is None else bool(r["at_me"]), r["created_at"],
                       raw, r["media_kind"], msg.media_msgsvrid if msg else "",
                       msg.media_source_talker if msg else r["sender"], r["media_path"],
                       msg.quote_text if msg else "", r["last_error"])

    def releasable_media(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self.conn.execute(
                "SELECT event_id,task_id FROM media_jobs "
                "WHERE status IN ('ready','failed','unknown') AND task_id<>'' AND release_after<=? "
                "ORDER BY release_after,updated_at,event_id LIMIT 24", (int(time.time()),))]

    def media_released(self, event_id: str, task_id: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE media_jobs SET task_id='' WHERE event_id=? AND task_id=? "
                              "AND status IN ('ready','failed','unknown')",
                              (event_id, task_id))

    def media_release_deferred(self, event_id: str, task_id: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE media_jobs SET release_after=? WHERE event_id=? AND task_id=?",
                              (int(time.time()) + 10, event_id, task_id))

    def pending_media(self, limit: int = 24) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("""
                SELECT i.event_id,i.owner,i.room,i.sender,i.kind,i.media_kind,i.raw_json,
                       j.request_id,j.task_id,j.status,j.error
                FROM inbox i JOIN media_jobs j ON j.event_id=i.event_id
                WHERE j.status IN ('new','pending') AND i.status='held_media' AND j.retry_after<=?
                ORDER BY CASE j.status WHEN 'pending' THEN 0 ELSE 1 END,i.sequence LIMIT ?
            """, (int(time.time()), limit)).fetchall()
            return [dict(r) for r in rows]

    def ready_file(self, owner: str, room: str, *, event_id: str = "", source_id: str = "") -> dict | None:
        """Exact account/conversation/ID association; never match by file name."""
        if not owner or not room or (not event_id and not source_id):
            return None
        with self._lock:
            rows = self.conn.execute(
                "SELECT i.event_id,i.raw_json,i.media_path,j.artifact_json FROM inbox i "
                "JOIN media_jobs j ON j.event_id=i.event_id "
                "WHERE i.owner=? AND i.room=? AND i.media_kind='file' AND j.status='ready' "
                "AND (?='' OR i.event_id=?) ORDER BY i.sequence DESC LIMIT 128",
                (owner, room, event_id, event_id),
            ).fetchall()
        for row in rows:
            try:
                raw = json.loads(row["raw_json"])
                source = normalize_event(raw, owner)
                artifact = json.loads(row["artifact_json"])
                if (source is None or source.media_kind != "file" or source.media_source_talker != room
                        or source_id and source.media_msgsvrid != source_id):
                    continue
                return {"path": row["media_path"], "artifact": artifact, "source": source}
            except (ValueError, TypeError):
                continue
        return None

    def begin_media(self, event_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute("UPDATE media_jobs SET status='requesting',updated_at=? "
                                    "WHERE event_id=? AND status='new'", (int(time.time()), event_id))
            return cur.rowcount == 1

    def media_capacity_wait(self, event_id: str) -> bool:
        """Retry only a definite admission rejection, at most 30 times."""
        with self._transaction():
            row = self.conn.execute("SELECT status,task_id,capacity_retries FROM media_jobs WHERE event_id=?",
                                    (event_id,)).fetchone()
            if row is None or row["status"] != "requesting" or row["task_id"]:
                return False
            if row["capacity_retries"] >= 30:
                self.media_failed(event_id, "media_capacity_wait_exhausted")
                return False
            now = int(time.time())
            self.conn.execute("UPDATE media_jobs SET status='new',error='media_capacity',"
                              "capacity_retries=capacity_retries+1,retry_after=?,updated_at=? WHERE event_id=?",
                              (now + 10, now, event_id))
            return True

    def media_pending(self, event_id: str, task_id: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE media_jobs SET status='pending',task_id=?,updated_at=? "
                              "WHERE event_id=? AND status='requesting'",
                              (task_id[:160], int(time.time()), event_id))

    def media_unknown(self, event_id: str, detail: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE media_jobs SET status='unknown',error=?,updated_at=? "
                              "WHERE event_id=?", (detail[:250], int(time.time()), event_id))
            self.conn.execute("UPDATE inbox SET last_error=? WHERE event_id=?", (detail[:250], event_id))

            # Missing media must remain visible without replaying a native download.
            self.conn.execute("UPDATE inbox SET status='pending' WHERE event_id=? "
                              "AND status='held_media'", (event_id,))

    def media_failed(self, event_id: str, detail: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE media_jobs SET status='failed',error=?,updated_at=? "
                              "WHERE event_id=?", (detail[:250], int(time.time()), event_id))
            self.conn.execute("UPDATE inbox SET last_error=? WHERE event_id=?", (detail[:250], event_id))
            self.conn.execute("UPDATE inbox SET status='pending' WHERE event_id=? AND status='held_media'", (event_id,))

    def media_ready(self, event_id: str, path: str, artifact: dict) -> None:
        with self._transaction():
            self.conn.execute("UPDATE media_jobs SET status='ready',artifact_json=?,error='',updated_at=? "
                              "WHERE event_id=?", (json.dumps(artifact, ensure_ascii=False), int(time.time()), event_id))
            self.conn.execute("UPDATE inbox SET status='pending',media_path=?,last_error='' "
                              "WHERE event_id=? AND status='held_media'", (path, event_id))

    def media_unavailable(self, event_id: str, detail: str) -> None:
        """Dispatch metadata with an explicit missing attachment, never raw XML."""
        with self._transaction():
            self.media_failed(event_id, detail)
            self.conn.execute("UPDATE inbox SET status='pending' WHERE event_id=? AND status='held_media'",
                              (event_id,))

    def mark_dispatched(self, event_id: str) -> bool:
        with self._lock:
            cur=self.conn.execute("UPDATE inbox SET status='dispatched' WHERE event_id=? AND status='pending'",(event_id,))
            return cur.rowcount==1

    def mark_dispatch_uncertain(self, event_id: str, reason: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE inbox SET status='interrupted_review', last_error=? "
                              "WHERE event_id=? AND status='dispatched'",
                              (("Queue handoff uncertain: "+str(reason))[:250],event_id))

    def mark_error(self, event_id: str, detail: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE inbox SET last_error=? WHERE event_id=?",
                              (detail[:250], event_id))

    def record_outbound(self, owner: str, room: str, content: str, state: str,
                        source_event_id: str = "", intent_id: str = "") -> int:
        """Persist the decision to reply, optionally idempotent across event replay.

        This only persists a record. Native submission is reserved separately;
        a historical draft is never promoted automatically.
        A stable intent_id belongs to one logical reply, not merely a text hash.
        """
        conversation_session(owner, room)
        if intent_id and (len(intent_id) > 128 or len(source_event_id) > 220
                          or not source_event_id):
            raise ValueError("Invalid outbound intent metadata")
        content = content[:30000]
        with self._lock:
            if intent_id:
                self.conn.execute(
                    "INSERT OR IGNORE INTO outbound(owner,room,content,status,created_at,intent_id,source_event_id) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (owner,room,content,state,int(time.time()),intent_id,source_event_id))
                row = self.conn.execute(
                    "SELECT id,owner,room,content,status,source_event_id FROM outbound "
                    "WHERE intent_id=?", (intent_id,)).fetchone()
                if row is None:
                    raise RuntimeError("Could not read persisted outbound intent")
                if (row["owner"] != owner or row["room"] != room
                        or row["content"] != content
                        or row["source_event_id"] != source_event_id):
                    self.conn.execute("""
                        INSERT OR IGNORE INTO outbound_conflicts
                        (intent_id,existing_outbound_id,requested_owner,requested_room,
                         requested_source_event_id,requested_content,detected_at,attempt_hash)
                        VALUES(?,?,?,?,?,?,?,?)
                    """, (intent_id, int(row["id"]), owner, room, source_event_id,
                          content, int(time.time()),
                          stable_hash([intent_id, owner, room, source_event_id, content])))
                    raise ValueError("Outbound intent already exists with different content or destination")

                return int(row["id"])
            cur = self.conn.execute(
                "INSERT INTO outbound(owner,room,content,status,created_at) VALUES(?,?,?,?,?)",
                (owner,room,content,state,int(time.time())))
            return int(cur.lastrowid)

    def outbound_state(self, record_id: int, state: str, task_id: str = "", error: str = "") -> None:
        with self._lock:
            self.conn.execute("UPDATE outbound SET status=?,task_id=?,error=? WHERE id=?",
                              (state, task_id[:160], error[:250], record_id))

    def stats(self) -> dict:
        with self._lock:
            counts = {r["status"]: r["count"] for r in self.conn.execute(
                "SELECT status,count(*) AS count FROM inbox GROUP BY status")}
            media = {r["status"]: r["count"] for r in self.conn.execute(
                "SELECT status,count(*) AS count FROM media_jobs GROUP BY status")}
            return {"cursor": self.after(), "inbox": counts, "media": media,
                    "drafts": self.conn.execute(
                        "SELECT count(*) FROM outbound WHERE status='draft_only'").fetchone()[0],
                    "draft_conflicts": self.conn.execute(
                        "SELECT count(*) FROM outbound_conflicts").fetchone()[0]}

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self.conn.close()
                self._closed = True
