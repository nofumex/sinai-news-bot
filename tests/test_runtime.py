import asyncio
import json
from unittest.mock import AsyncMock
import httpx
import pytest
from sinai.app import create_app
from sinai.config import Settings
from sinai.crm import CRM
from sinai.db import stamp
from sinai.http import APIError
from conftest import tg_update, registered
from test_api_clients import mock_http


async def test_lifespan_migrations_readiness_and_worker(tmp_path):
    config = Settings(_env_file=None, database_path=tmp_path/'runtime.sqlite3', tg_webhook_secret='local-secret')
    app = create_app(config)
    app.state.service.crm.verify_pipeline = AsyncMock(return_value={'id': 8993570})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://localhost') as client:
            assert (await client.get('/health/ready')).status_code == 200
            response = await client.post('/webhooks/telegram', headers={'X-Telegram-Bot-Api-Secret-Token': 'local-secret'}, json=tg_update('/start'))
            assert response.status_code == 200
            for _ in range(30):
                sub = await app.state.db.one("SELECT * FROM subscriptions")
                if sub and await app.state.db.one("SELECT * FROM jobs WHERE kind='message'"):
                    break
                await asyncio.sleep(.05)
            assert sub['user_id'] == 50
            assert await app.state.db.one("SELECT * FROM jobs WHERE kind='message'")


async def test_opt_out_is_applied_before_slow_worker_can_send(tmp_path):
    config = Settings(_env_file=None, database_path=tmp_path/'stop.sqlite3', tg_webhook_secret='local-secret')
    app = create_app(config)
    db = app.state.db
    await db.open()
    try:
        await db.execute("INSERT INTO clients(contact_id,name,segment,subscribed_at,preferred_platform,segment_source,created_at) VALUES(101,'Иван','active',?,'telegram','crm',?)", (stamp(), stamp()))
        await db.execute("INSERT INTO subscriptions(platform,user_id,chat_id,client_id,state,created_at) VALUES('telegram',50,50,1,'confirmed',?)", (stamp(),))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://localhost') as client:
            response = await client.post('/webhooks/telegram', headers={'X-Telegram-Bot-Api-Secret-Token': 'local-secret'}, json=tg_update('/stop'))
            assert response.status_code == 200
        assert (await db.one("SELECT * FROM clients"))['opted_out'] == 1
        assert (await db.one("SELECT * FROM subscriptions"))['stopped'] == 1
        assert (await db.one("SELECT * FROM jobs"))['state'] == 'pending'
    finally:
        await db.close()
        await app.state.service.crm.http.close()
        for platform in app.state.service.platforms.values():
            await platform.http.close()


async def test_unknown_note_not_recreated_when_marker_absent(system):
    key = 'uncertain'
    await system.db.execute("INSERT INTO crm_notes(key,lead_id,text,state,created_at) VALUES(?,201,'note','unknown',?)", (key, stamp()))
    system.config.test_mode = False
    system.config.crm_notes_enabled = True
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(204)
    crm = CRM(system.config, system.db, mock_http(handler))
    try:
        with pytest.raises(APIError):
            await crm.write_note(key)
        assert calls == ['GET']
    finally:
        await crm.http.close()


async def test_notes_post_common_to_existing_deal_only(system):
    await system.db.execute("INSERT INTO crm_notes(key,lead_id,text,created_at) VALUES('key',201,'Клиент подписался',?)", (stamp(),))
    system.config.test_mode = False
    system.config.crm_notes_enabled = True
    seen = []
    def handler(request):
        seen.append(request)
        if request.method == 'GET':
            return httpx.Response(204)
        return httpx.Response(200, json={'_embedded': {'notes': [{'id': 300}]}})
    crm = CRM(system.config, system.db, mock_http(handler))
    try:
        await crm.write_note('key')
        assert [x.method for x in seen] == ['GET', 'POST']
        assert seen[1].url.path == '/api/v4/leads/201/notes'
        payload = json.loads(seen[1].content)
        assert payload[0]['note_type'] == 'common' and '[sinai:key]' in payload[0]['params']['text']
    finally:
        await crm.http.close()


async def test_incomplete_pagination_fails_closed(system):
    system.config.max_contact_pages = 1
    crm = CRM(system.config, system.db, mock_http(lambda request: httpx.Response(200, json={'_embedded': {'contacts': [{'id': 101}]}, '_links': {'next': {'href': 'next'}}})))
    try:
        with pytest.raises(APIError):
            await crm.refresh_index()
        assert not await system.db.all("SELECT * FROM contact_index")
    finally:
        await crm.http.close()


async def test_api_outage_prevents_send(system):
    from dateutil.relativedelta import relativedelta
    from sinai.db import now
    await registered(system, at=now()-relativedelta(months=3, minutes=10))
    system.config.test_mode = False
    system.config.broadcasts_enabled = True
    system.platforms['telegram'].send = AsyncMock(return_value='unused')
    await system.scheduler.tick()
    system.crm.verify_pipeline = AsyncMock(side_effect=APIError('amoCRM',503))
    with pytest.raises(APIError):
        await system.scheduler.deliver((await system.db.one("SELECT * FROM deliveries"))['id'])
    system.platforms['telegram'].send.assert_not_awaited()


async def test_replacing_removed_lead_does_not_reopen_manual_review(system):
    sub = await registered(system)
    system.crm.contacts[101]['_embedded']['leads'] = [{'id': 202}]
    system.crm.leads[202] = dict(system.crm.leads[201], id=202)
    await system.service.reconcile_client(sub['client_id'])
    review = await system.db.one("SELECT * FROM reviews WHERE status='pending'")
    fresh = await system.db.one("SELECT * FROM subscriptions WHERE id=?", (sub['id'],))
    await system.service.bind(fresh, {'contact_id': 101, 'lead_id': 202}, 'manual', 'admin', review['id'])
    await system.service.reconcile_client(sub['client_id'])
    assert not await system.db.one("SELECT * FROM reviews WHERE status='pending'")
    assert [x['lead_id'] for x in await system.db.all("SELECT * FROM bindings")] == [202]
    assert await system.db.one("SELECT * FROM events WHERE kind='bindings_replaced'")


async def test_safe_get_retries_5xx_with_backoff():
    from unittest.mock import patch
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(503 if count < 3 else 200, json={'id': 101})
    http = mock_http(handler)
    try:
        with patch('sinai.http.asyncio.sleep', new_callable=AsyncMock) as sleeper:
            result = await http.request('GET', 'https://test/read')
            assert count == 3 and result['id'] == 101
            assert any(call.args[0] >= 1 for call in sleeper.await_args_list)
    finally:
        await http.close()
