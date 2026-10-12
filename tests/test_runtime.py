import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from filelock import Timeout
from sinai.app import Runtime
from sinai.config import Settings
from sinai.db import Database,stamp


async def test_runtime_has_no_http_server_and_shuts_down_cleanly(tmp_path):
    runtime = Runtime(Settings(_env_file=None,database_path=tmp_path/'runtime.sqlite3',polling_enabled=False))
    runtime.crm.verify_pipeline = AsyncMock(return_value={'id':8993570})
    async with runtime.lifespan():
        assert len(runtime.tasks) == 1
        await asyncio.sleep(.05)
        assert await runtime.db.one('SELECT 1')
    assert all(t.done() for t in runtime.tasks)


async def test_only_one_runtime_can_own_database(tmp_path):
    config = Settings(_env_file=None,database_path=tmp_path/'locked.sqlite3')
    first,second = Runtime(config),Runtime(config)
    first.crm.verify_pipeline = AsyncMock()
    async with first.lifespan():
        with pytest.raises(Timeout):
            async with second.lifespan():
                pass
    await second.crm.http.close()
    for p in second.platforms.values():
        await p.http.close()
        await p.poll_http.close()


async def test_upgrade_preserves_old_clients_ids_history_and_foreign_keys(tmp_path):
    path = tmp_path/'old.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT)')
        for file in sorted(Path('sinai/migrations').glob('00[12]*.sql')):
            conn.executescript(file.read_text(encoding='utf-8'))
            conn.execute('INSERT INTO schema_migrations VALUES(?,?)',(file.stem,stamp()))
        conn.execute("INSERT INTO clients VALUES(10,101,'Иван','active',?,'telegram',0,'crm',NULL,?)",(stamp(),stamp()))
        conn.execute("INSERT INTO subscriptions(id,platform,user_id,chat_id,client_id,phone,selected_segment,state,subscribed_at,created_at) VALUES(20,'telegram',50,50,10,'+79991234567','active','confirmed',?,?)",(stamp(),stamp()))
        conn.execute("INSERT INTO bindings(subscription_id,contact_id,lead_id,method,created_at) VALUES(20,101,201,'phone',?)",(stamp(),))
        conn.execute("INSERT INTO deliveries(client_id,subscription_id,cycle,segment,text,state,due_at,created_at) VALUES(10,20,1,'active','text','sent',?,?)",(stamp(),stamp()))
        conn.execute("INSERT INTO events(kind,subscription_id,client_id,created_at) VALUES('old',20,10,?)",(stamp(),))
        conn.commit()
    db = Database(path)
    await db.open()
    try:
        assert (await db.one('SELECT * FROM clients WHERE id=10'))['contact_id'] == 101
        assert (await db.one('SELECT * FROM subscriptions WHERE id=20'))['claimed_lead_id'] == 201
        assert (await db.one('SELECT * FROM lead_claims'))['subscription_id'] == 20
        assert (await db.one('SELECT * FROM deliveries'))['state'] == 'sent'
        assert (await db.one('SELECT * FROM events'))['kind'] == 'old'
        assert not await db.all('PRAGMA foreign_key_check')
        assert (await db.one('PRAGMA foreign_keys'))['foreign_keys'] == 1
    finally:
        await db.close()
