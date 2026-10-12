import asyncio
import json
import os
import sqlite3
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
        self.owner = None
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
                rebuild = file.stem == "003_polling_registration_campaigns"
                if rebuild:
                    await self.execute("PRAGMA foreign_keys=OFF")
                try:
                    await self.execute("BEGIN IMMEDIATE")
                    # Recheck inside the SQLite write lock for concurrent migration callers.
                    if not await self.one("SELECT version FROM schema_migrations WHERE version=?", (file.stem,)):
                        statement = ""
                        for line in file.read_text(encoding="utf-8").splitlines(True):
                            statement += line
                            if sqlite3.complete_statement(statement):
                                await self.execute(statement)
                                statement = ""
                        if statement.strip():
                            raise ValueError("Incomplete migration statement")
                        if await self.all("PRAGMA foreign_key_check"):
                            raise ValueError("Migration foreign key check failed")
                        await self.execute("INSERT INTO schema_migrations VALUES(?,?)", (file.stem, stamp()))
                    await self.execute("COMMIT")
                except BaseException:
                    await self.execute("ROLLBACK")
                    raise
                finally:
                    if rebuild:
                        await self.execute("PRAGMA foreign_keys=ON")

    async def canonical(self, client_id):
        for _ in range(100):
            row = await self.one("SELECT * FROM clients WHERE id=?", (client_id,))
            if not row or row["merged_into"] is None:
                return row
            client_id = row["merged_into"]
        raise ValueError("Client merge cycle")

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def execute(self, sql, args=()):
        if self.owner is asyncio.current_task():
            return await self.conn.execute(sql, args)
        async with self.lock:
            return await self.conn.execute(sql, args)

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
            self.owner = asyncio.current_task()
            try:
                await self.execute("BEGIN IMMEDIATE")
                yield self
                await self.execute("COMMIT")
            except BaseException:
                await self.execute("ROLLBACK")
                raise
            finally:
                self.owner = None

    async def family(self, client_id):
        root = await self.canonical(client_id)
        if not root:
            return []
        rows = await self.all('WITH RECURSIVE family(id) AS (SELECT ? UNION ALL SELECT c.id FROM clients c JOIN family f ON c.merged_into=f.id) SELECT id FROM family', (root['id'],))
        return [r['id'] for r in rows]

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
