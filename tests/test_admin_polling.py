import asyncio
from unittest.mock import AsyncMock
import httpx
from sinai.polling import Poller,event_key
from sinai.http import HTTP
from conftest import registered,tg_update,messages


async def test_admin_unsynced_pagination_and_binding(system):
    for uid in range(10,18):
        await registered(system,uid=uid,number=None)
    uid = system.config.admin_id
    await system.service.handle('telegram',tg_update(callback='admin:unsynced:0',uid=uid),'page')
    page = (await messages(system))[-1]
    assert page['text'].count('telegram') == 5
    assert any(b.get('data') == 'admin:unsynced:1' for row in page['buttons'] for b in row)
    sid = (await system.db.one('SELECT id FROM subscriptions ORDER BY id'))['id']
    await system.service.handle('telegram',tg_update(callback=f'admin:attach:{sid}',uid=uid),'ask')
    await system.service.handle('telegram',tg_update('201',uid=uid),'enter')
    sub = await system.db.one('SELECT * FROM subscriptions WHERE id=?',(sid,))
    assert sub['claimed_lead_id'] == 201 and sub['crm_method'] == 'admin'
    assert len(await system.db.all('SELECT * FROM crm_notes')) == 1
    await system.service.handle('telegram',tg_update(callback=f'admin:attach:{sid}',uid=uid),'ask2')
    await system.service.handle('telegram',tg_update('201',uid=uid),'enter2')
    assert len(await system.db.all('SELECT * FROM crm_notes')) == 1


async def test_admin_access_denied(system):
    for command in ['/admin','/message']:
        await system.service.handle('telegram',tg_update(command),command)
        assert (await messages(system))[-1]['text'] == 'Доступ запрещён.'


async def test_batch_cursor_and_updates_are_atomic_and_deduplicated(system):
    poller = Poller(system.config,system.db,system.service,'telegram')
    updates = [tg_update('/start',update_id=11),tg_update('/start',uid=70,update_id=12)]
    await asyncio.gather(poller.ingest(updates,13),poller.ingest(updates,13))
    assert len(await system.db.all("SELECT * FROM jobs WHERE kind='inbox'")) == 2
    assert await system.db.setting('poll_cursor:telegram',None) == '13'
    original = system.db.enqueue
    async def fail(*args,**kwargs):
        await original(*args,**kwargs)
        raise RuntimeError('simulate disk failure')
    system.db.enqueue = fail
    import pytest
    with pytest.raises(RuntimeError):
        await poller.ingest([tg_update('/start',update_id=15)],16)
    assert await system.db.setting('poll_cursor:telegram',None) == '13'
    assert not await system.db.one('SELECT * FROM jobs WHERE key=?',('telegram:15',))


async def test_pollers_restore_offset_and_marker(system):
    for platform,cursor in [('telegram',101),('max',555)]:
        await system.db.set_setting('poll_cursor:'+platform,cursor)
        system.platforms[platform].poll = AsyncMock(return_value=([],cursor+1))
        await Poller(system.config,system.db,system.service,platform).once()
        system.platforms[platform].poll.assert_awaited_once_with(str(cursor))
        assert await system.db.setting('poll_cursor:'+platform,None) == str(cursor+1)


async def test_polling_api_parameters_and_marker_bootstrap(system):
    calls = []
    def handler(request):
        import json
        calls.append(request)
        if request.url.host == 'api.telegram.org':
            data = json.loads(request.content)
            assert data['offset'] == 100 and data['timeout'] == 25
            return httpx.Response(200,json={'ok':True,'result':[tg_update('/start',update_id=100)]})
        assert request.headers['Authorization'] == 'fake-max-token'
        return httpx.Response(200,json={'updates':[],'marker':123})
    for platform in system.platforms.values():
        await platform.poll_http.close()
        platform.poll_http = HTTP('test',client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),interval=0)
    assert (await system.platforms['telegram'].poll('100'))[1] == 101
    assert (await system.platforms['max'].poll(None))[1] == 123
    assert 'marker' not in calls[-1].url.params
    await system.platforms['max'].poll('123')
    assert calls[-1].url.params['marker'] == '123'


async def test_stop_is_applied_during_ingestion_before_worker(system):
    sub = await registered(system)
    await Poller(system.config,system.db,system.service,'telegram').ingest([tg_update('/stop',update_id=40)],41)
    assert (await system.db.one('SELECT * FROM clients WHERE id=?',(sub['client_id'],)))['opted_out'] == 1


def test_max_dedup_identity():
    assert event_key('max',{'update_type':'message_created','timestamp':1,'message':{'body':{'mid':'a'}}}) == event_key('max',{'update_type':'message_created','timestamp':2,'message':{'body':{'mid':'a'}}})


async def test_stats_does_not_claim_unverified_accounts_are_unique_people(system):
    await registered(system)
    await registered(system,platform='max',uid=70)
    await registered(system,uid=80,number=None)
    await system.service.handle('telegram',tg_update(callback='admin:stats:all',uid=system.config.admin_id),'stats')
    text = (await messages(system))[-1]['text']
    assert 'Уникальные подтверждённые CRM-клиенты: 1' in text
    assert 'Аккаунты Telegram / MAX: 2 / 1' in text


async def test_polling_conflict_stops_runtime_instead_of_retrying_forever(system):
    import pytest
    from sinai.http import APIError
    poller = Poller(system.config,system.db,system.service,'telegram')
    system.platforms['telegram'].poll = AsyncMock(side_effect=APIError('telegram',409))
    with pytest.raises(APIError):
        await poller.run()
    assert await system.db.setting('poll_cursor:telegram',None) is None
    system.platforms['telegram'].poll.assert_awaited_once()
