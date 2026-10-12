import asyncio
from unittest.mock import AsyncMock
import httpx
from sinai.app import create_app, amo_lead_ids, event_key
from sinai.config import Settings
from conftest import registered, subscriber, tg_update, messages


async def test_admin_access_and_pagination(system):
    await system.service.handle('telegram', tg_update('/admin'), 'denied')
    assert (await messages(system))[-1]['text'] == 'Доступ запрещён.'
    for uid in range(60, 72):
        await subscriber(system, uid=uid)
    await system.service.handle('telegram', tg_update(callback='admin:subscribers:0', uid=system.config.admin_id), 'page')
    result = (await messages(system))[-1]
    assert 'страница 1' in result['text']
    assert result['text'].count('Без ФИО') == 5
    assert any(b.get('data') == 'admin:subscribers:1' for row in result['buttons'] for b in row)


async def test_stats_separates_unique_clients_from_accounts(system):
    await registered(system)
    await registered(system, platform='max', uid=70)
    await system.service.handle('telegram', tg_update(callback='admin:stats:all', uid=system.config.admin_id), 'stats')
    text = (await messages(system))[-1]['text']
    assert 'Уникальные подтверждённые клиенты: 1' in text
    assert 'Подписки Telegram / MAX: 1 / 1' in text


async def test_manual_confirmation_idempotent_and_snapshots_phone(system):
    sid = await subscriber(system, number='+79990000000')
    await system.db.execute("UPDATE subscriptions SET name='Иванов Иван Иванович' WHERE id=?", (sid,))
    await system.service.sync(sid, by_name=True)
    review = await system.db.one("SELECT * FROM reviews")
    callback = f"admin:confirm:{review['id']}:{review['version']}:0"
    update = tg_update(callback=callback, uid=system.config.admin_id)
    await system.service.handle('telegram', update, 'confirm1')
    await system.service.handle('telegram', update, 'confirm2')
    assert len(await system.db.all("SELECT * FROM bindings")) == 1
    assert len(await system.db.all("SELECT * FROM events WHERE kind='matched_manual'")) == 1
    client = await system.db.one("SELECT * FROM clients")
    await system.service.reconcile_client(client['id'])
    assert (await system.db.one("SELECT * FROM subscriptions"))['state'] == 'confirmed'


async def test_stale_admin_candidate_button_rejected(system):
    sid = await subscriber(system, number='+79990000000')
    await system.db.execute("UPDATE subscriptions SET name='Иванов Иван Иванович' WHERE id=?", (sid,))
    await system.service.sync(sid, by_name=True)
    review = await system.db.one("SELECT * FROM reviews")
    await system.service.sync(sid, by_name=True)
    await system.service.handle('telegram', tg_update(callback=f"admin:confirm:{review['id']}:{review['version']}:0", uid=system.config.admin_id), 'stale')
    assert not await system.db.all("SELECT * FROM clients")


def test_amo_form_events_and_json():
    assert amo_lead_ids(b'leads%5Bstatus%5D%5B0%5D%5Bid%5D=201&leads%5Bupdate%5D%5B0%5D%5Bid%5D=201', 'application/x-www-form-urlencoded') == [201]
    assert amo_lead_ids(b'{"leads":{"update":[{"id":"202","pipeline_id":8993570}]}}', 'application/json') == [202]


def test_max_dedup_uses_mid_and_callback_id():
    assert event_key('max', {'update_type': 'message_created', 'timestamp': 1, 'message': {'body': {'mid': 'mid'}}}) == event_key('max', {'update_type': 'message_created', 'timestamp': 2, 'message': {'body': {'mid': 'mid'}}})
    assert event_key('max', {'callback': {'callback_id': 'cb'}}) == 'max:callback:cb'


async def test_webhook_secret_dedup_and_size_limit(tmp_path):
    config = Settings(_env_file=None, database_path=tmp_path/'web.sqlite3', tg_webhook_secret='test-secret', amo_webhook_secret='amo-secret')
    app = create_app(config)
    await app.state.db.open()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://localhost') as client:
            denied = await client.post('/webhooks/telegram', json=tg_update('/start'))
            assert denied.status_code == 403
            headers = {'X-Telegram-Bot-Api-Secret-Token': 'test-secret'}
            await asyncio.gather(*[client.post('/webhooks/telegram', json=tg_update('/start'), headers=headers) for _ in range(5)])
            assert len(await app.state.db.all("SELECT * FROM jobs")) == 1
            assert (await client.post('/webhooks/telegram', headers=headers, content=b'x'*262145)).status_code == 413
            amo_body = 'leads[status][0][id]=201'
            for _ in range(2):
                response = await client.post('/webhooks/amo/amo-secret', content=amo_body, headers={'Content-Type': 'application/x-www-form-urlencoded'})
                assert response.status_code == 200
            assert len(await app.state.db.all("SELECT * FROM jobs WHERE kind='reconcile_lead'")) == 1
    finally:
        await app.state.db.close()
        await app.state.service.crm.http.close()
        for platform in app.state.service.platforms.values():
            await platform.http.close()


async def test_webhook_pipeline_value_is_never_trusted(system):
    await registered(system)
    await system.service.reconcile_lead(201)
    assert (await system.db.one("SELECT * FROM clients"))['segment'] == 'active'
    system.crm.leads[201]['pipeline_id'] = 8993570
    await system.service.reconcile_lead(201)
    assert (await system.db.one("SELECT * FROM clients"))['segment'] == 'completed'


async def test_queue_retry_and_recovery(system):
    from sinai.http import RetryLater
    await system.db.enqueue('test-job', 'pipeline', {})
    system.crm.verify_pipeline = AsyncMock(side_effect=RetryLater('amoCRM', 60))
    await system.worker.run_one()
    job = await system.db.one("SELECT * FROM jobs WHERE key='test-job'")
    assert job['state'] == 'pending' and job['attempts'] == 1
    assert await system.db.one("SELECT * FROM errors")
    await system.db.execute("UPDATE jobs SET state='running' WHERE id=?", (job['id'],))
    await system.worker.recover()
    system.crm.verify_pipeline.side_effect = None
    await system.worker.run_one()
    assert (await system.db.one("SELECT * FROM jobs WHERE id=?", (job['id'],)))['state'] == 'done'
    assert (await system.db.one("SELECT * FROM errors"))['resolved_at']


async def test_max_null_body_is_ignored(system):
    assert system.platforms['max'].parse({'update_type': 'message_created', 'message': {'recipient': {'chat_type': 'dialog'}, 'sender': {'user_id': 50}, 'body': None}}).contact is None
