"""Instance-owned Inbox paths and an OS-released exclusive transport lease."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path


LEGACY_INBOX = Path("data/wechat_hook_inbox.sqlite3")


def inbox_path(config: dict, owner: str) -> Path:
    configured = str(config.get("inbox_db") or "").strip()
    legacy = LEGACY_INBOX.resolve()
    if configured and Path(configured).expanduser().resolve() != legacy:
        return Path(configured).expanduser().resolve()
    # Keep the first existing account/instance on its original database.
    if legacy.is_file():
        with sqlite3.connect(legacy.as_uri() + "?mode=ro", uri=True) as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            binding = (
                conn.execute(
                    "SELECT instance_id,owner FROM transport_binding"
                ).fetchone()
                if "transport_binding" in tables
                else None
            )
            owners = {
                r[0]
                for table in ("inbox", "outbound")
                if table in tables
                for r in conn.execute(f"SELECT DISTINCT owner FROM {table}")
            }
        if owners <= {owner} and (
            binding is None or binding == (str(config["id"]), owner)
        ):
            return legacy
    key = hashlib.sha256((str(config["id"]) + "\x1f" + owner).encode()).hexdigest()[:24]
    return Path("data/wechat_hook") / key / "inbox.sqlite3"


class InboxLease:
    """Only the connector takes this lease; read-only/Agent DB users need not."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = path.with_suffix(path.suffix + ".lock").open("a+b")
        try:
            self.stream.seek(0, 2)
            if self.stream.tell() == 0:
                self.stream.write(b"\0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise RuntimeError(
                f"Inbox is already owned by another running connector: {path}"
            ) from exc

    def close(self) -> None:
        if self.stream.closed:
            return
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()


def bind_inbox(inbox, instance_id: str, owner: str) -> None:
    with inbox._transaction():
        conn = inbox.conn
        conn.execute(
            "CREATE TABLE IF NOT EXISTS transport_binding (singleton INTEGER PRIMARY KEY CHECK(singleton=1),instance_id TEXT NOT NULL,owner TEXT NOT NULL)"
        )
        binding = conn.execute(
            "SELECT instance_id,owner FROM transport_binding"
        ).fetchone()
        if binding is not None and tuple(binding) != (instance_id, owner):
            raise ValueError(
                "Inbox belongs to another robot/account; choose a separate inbox_db"
            )
        owners = {
            r[0]
            for table in ("inbox", "outbound")
            for r in conn.execute(f"SELECT DISTINCT owner FROM {table}")
        }
        if owners - {owner}:
            raise ValueError(
                "Inbox contains another account's data; choose a separate inbox_db"
            )
        conn.execute(
            "INSERT OR IGNORE INTO transport_binding VALUES(1,?,?)",
            (instance_id, owner),
        )
