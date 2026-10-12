import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import aiosqlite


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime | None = None) -> str:
    return (value or now()).astimezone(timezone.utc).isoformat()


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.conn = None

    async def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        os.chmod(self.path, 0o600)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.execute("PRAGMA busy_timeout=10000")
        await self.conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY, applied_at TEXT)")
        for file in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
            if not await self.one("SELECT version FROM schema_migrations WHERE version=?", (file.stem,)):
                script = "BEGIN IMMEDIATE;\n" + file.read_text(encoding="utf-8")
                script += f"\nINSERT INTO schema_migrations VALUES ('{file.stem}', '{stamp()}');\nCOMMIT;"
                await self.conn.executescript(script)

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def execute(self, sql, args=()):
        cursor = await self.conn.execute(sql, args)
        return cursor

    async def one(self, sql, args=()):
        cursor = await self.execute(sql, args)
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def all(self, sql, args=()):
        cursor = await self.execute(sql, args)
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(x) for x in rows]

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            await self.execute("BEGIN IMMEDIATE")
            try:
                yield self
                await self.execute("COMMIT")
            except BaseException:
                await self.execute("ROLLBACK")
                raise

    async def event(self, kind, sub_id=None, client_id=None, detail=None):
        await self.execute("INSERT INTO events(kind,subscription_id,client_id,detail,created_at) VALUES(?,?,?,?,?)",
                           (kind, sub_id, client_id, dumps(detail or {}), stamp()))

    async def enqueue(self, key, kind, payload, at=None):
        await self.execute("INSERT OR IGNORE INTO jobs(key,kind,payload,available_at,created_at) VALUES(?,?,?,?,?)",
                           (key, kind, dumps(payload), stamp(at), stamp()))

    async def setting(self, key, fallback):
        row = await self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else fallback

    async def set_setting(self, key, value):
        await self.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (key, str(value)))
