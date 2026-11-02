"""Read-only nickname fallback for contact schemas reported by the running Hook."""
import time

from astrbot.api import logger


def identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def column(columns, *aliases):
    names = {name.replace("_", "").lower(): name for name in columns}
    return next((names[key] for key in aliases if key in names), None)


class ContactDatabase:
    def __init__(self, lookup):
        self.lookup = lookup
        self.schemas = []
        self.expires = 0

    async def query(self, db: str, sql: str):
        if not (sql.startswith("SELECT ") or sql.startswith("PRAGMA table_info(")):
            raise ValueError("Contact fallback only accepts read-only schema/name queries")
        response = await self.lookup("/QueryDB/execute", {"optDbName": db, "SQL": sql})
        if not isinstance(response, dict) or str(response.get("status", response.get("ret"))) != "0":
            status = response.get("status", response.get("ret")) if isinstance(response, dict) else "unavailable"
            logger.debug(f"Hook contact database query unavailable: db={db}, status={status}")
            return []
        rows = response.get("data")
        return [row for row in rows[:200] if isinstance(row, dict)] if isinstance(rows, list) else []

    async def discover(self):
        self.schemas = []
        self.expires = time.monotonic() + 60
        response = await self.lookup("/QueryDB/GetAllDBName", {})
        rows = response if isinstance(response, list) else response.get("data", []) if isinstance(response, dict) else []
        names = []
        for row in rows[:128] if isinstance(rows, list) else []:
            name = row.get("dbName") if isinstance(row, dict) else None
            if isinstance(name, str) and 0 < len(name) <= 128 and "\x00" not in name:
                lower = name.lower()
                if lower.endswith(".db") and ("contact" in lower or lower == "micromsg.db"):
                    names.append(name)
        names = sorted(set(names), key=lambda name: (name.lower() != "contact.db", name.lower()))[:4]
        if not names:
            logger.warning("Hook nickname fallback unavailable: no contact database reported by /QueryDB/GetAllDBName")
        for db in names:
            tables = await self.query(db, "SELECT name FROM sqlite_master WHERE type='table' AND lower(name) LIKE '%contact%' ORDER BY CASE lower(name) WHEN 'contact' THEN 0 ELSE 1 END, name LIMIT 8")
            for row in tables:
                table = row.get("name")
                if not isinstance(table, str) or not 0 < len(table) <= 128 or "\x00" in table:
                    continue
                metadata = await self.query(db, "PRAGMA table_info(" + identifier(table) + ")")
                fields = [entry["name"] for entry in metadata if isinstance(entry.get("name"), str) and "\x00" not in entry["name"]]
                user = column(fields, "username", "wxid")
                nickname = column(fields, "nickname", "displayname")
                if user and nickname:
                    self.schemas.append((db, table, user, nickname))
                    logger.info(f"Hook nickname schema selected: db={db}, table={table}, user_column={user}, nickname_column={nickname}")
                else:
                    logger.debug(f"Hook contact table has no compatible nickname columns: db={db}, table={table}, columns={fields}")
        if self.schemas:
            self.expires = time.monotonic() + 600

    async def resolve(self, users):
        if not users:
            return {}
        if time.monotonic() >= self.expires:
            await self.discover()
        remaining, names = set(users[:200]), {}
        for db, table, user, nickname in self.schemas:
            if not remaining:
                break
            sql = ("SELECT " + identifier(user) + " AS UserName, " + identifier(nickname)
                   + " AS NickName FROM " + identifier(table) + " WHERE " + identifier(user)
                   + " IN (" + ",".join(literal(uid) for uid in sorted(remaining)) + ") LIMIT 200")
            for row in await self.query(db, sql):
                uid, name = row.get("UserName"), row.get("NickName")
                if isinstance(uid, str) and uid in remaining and isinstance(name, str) and name.strip() and name != uid:
                    names[uid] = name
                    remaining.remove(uid)
        if names:
            logger.info(f"Hook contact database nicknames resolved: count={len(names)}")
        return names
