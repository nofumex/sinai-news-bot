import json
import httpx
import pytest
from sinai.config import Settings
from sinai.crm import CRM
from sinai.db import stamp
from sinai.http import HTTP, AmbiguousSend, RetryLater
from conftest import crm_contact


def mock_http(handler, service='amoCRM'):
    return HTTP(service, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), interval=0)


async def test_phone_query_verifies_actual_phone_and_paginates(system):
    seen = []
    def handler(request):
        seen.append(str(request.url))
        if request.url.params['page'] == '1':
            return httpx.Response(200, json={'_embedded': {'contacts': [crm_contact(102, number='79990000000')]}, '_links': {'next': {'href': 'ignored'}}})
        return httpx.Response(200, json={'_embedded': {'contacts': [crm_contact()]}})
    crm = CRM(system.config, system.db, mock_http(handler))
    try:
        results = await crm.by_phone('+79991234567')
        assert [x['id'] for x in results] == [101]
        assert any('page=2' in x for x in seen)
    finally:
        await crm.http.close()


async def test_name_fuzzy_scan_and_local_ranking(system):
    def handler(request):
        return httpx.Response(200, json={'_embedded': {'contacts': [crm_contact(), crm_contact(102, 'Смирнов Петр Петрович')]}})
    crm = CRM(system.config, system.db, mock_http(handler))
    try:
        results = await crm.by_name('Иваноф Иван Иванович')
        assert results[0]['id'] == 101
        assert results[0]['match_score'] > .9
    finally:
        await crm.http.close()


async def test_crm_forbids_creation_and_mutation(system):
    crm = CRM(system.config, system.db)
    try:
        for method, path in [('POST', '/api/v4/leads'), ('POST', '/api/v4/contacts'), ('PATCH', '/api/v4/leads/201')]:
            with pytest.raises(ValueError):
                await crm.request(method, path, json={})
    finally:
        await crm.http.close()


async def test_note_marker_avoids_duplicate_after_crash(system):
    key = 'subscription-1-contact-101-lead-201'
    await system.db.execute("INSERT INTO crm_notes(key,lead_id,text,created_at,state) VALUES(?,?,?,?,'unknown')", (key, 201, 'hello', stamp()))
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={'_embedded': {'notes': [{'id': 900, 'params': {'text': 'hello\n[sinai:' + key + ']'}}]}})
    system.config.test_mode = False
    system.config.crm_notes_enabled = True
    crm = CRM(system.config, system.db, mock_http(handler))
    try:
        await crm.write_note(key)
        assert calls == ['GET']
        assert (await system.db.one("SELECT * FROM crm_notes"))['state'] == 'sent'
    finally:
        await crm.http.close()


async def test_oauth_refresh_atomic_and_single_under_concurrency(system, tmp_path):
    config = Settings(_env_file=None, env_path=tmp_path/'.env', amocrm_access_token='old', amocrm_refresh_token='refresh-old',
                      amocrm_client_id='id', amocrm_client_secret='secret', amocrm_redirect_uri='https://app.test/oauth')
    config.env_path.write_text('OTHER_SETTING=preserved\n', encoding='utf-8')
    refreshes = []
    def handler(request):
        if request.url.path == '/oauth2/access_token':
            refreshes.append(json.loads(request.content))
            return httpx.Response(200, json={'access_token': 'new', 'refresh_token': 'refresh-new'})
        if request.headers['Authorization'] == 'Bearer old':
            return httpx.Response(401)
        return httpx.Response(200, json={'id': 101})
    crm = CRM(config, system.db, mock_http(handler))
    try:
        import asyncio
        await asyncio.gather(*[crm.contact(101) for _ in range(4)])
        assert len(refreshes) == 1
        persisted = config.env_path.read_text(encoding='utf-8')
        assert "AMOCRM_ACCESS_TOKEN='new'" in persisted and "AMOCRM_REFRESH_TOKEN='refresh-new'" in persisted
        assert 'OTHER_SETTING=preserved' in persisted
    finally:
        await crm.http.close()


async def test_http_send_429_and_5xx_are_distinguished():
    http = mock_http(lambda request: httpx.Response(429, json={'parameters': {'retry_after': 42}}), 'telegram')
    with pytest.raises(RetryLater) as caught:
        await http.request('POST', 'https://test/send', safe=False)
    assert caught.value.seconds == 42
    await http.close()
    calls = []
    def fail(request):
        calls.append(request)
        return httpx.Response(503)
    http = mock_http(fail, 'max')
    with pytest.raises(AmbiguousSend):
        await http.request('POST', 'https://test/send', safe=False)
    assert len(calls) == 1
    await http.close()


async def test_platform_contact_keyboard_formats(system):
    system.config.test_mode = False
    seen = []
    def handler(request):
        seen.append((str(request.url), json.loads(request.content), request.headers.get('Authorization')))
        if request.url.host == 'api.telegram.org':
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 9}})
        return httpx.Response(200, json={'message': {'body': {'mid': 'max-mid'}}})
    for platform in system.platforms.values():
        await platform.http.close()
        platform.http = mock_http(handler, 'platform')
        await platform.send(50, 50, 'Контакт', request_contact=True)
    assert seen[0][1]['reply_markup']['keyboard'][0][0]['request_contact'] is True
    assert seen[1][1]['attachments'][0]['payload']['buttons'][0][0]['type'] == 'request_contact'
    assert seen[1][0].startswith('https://platform-api2.max.ru/messages?')
    assert seen[1][2] == 'fake-max-token'
