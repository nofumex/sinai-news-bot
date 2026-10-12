import asyncio
import hashlib
import hmac
import pytest
from sinai.db import Database
from sinai.matching import phone
from sinai.platforms.base import Incoming
from sinai.service import Service,UNAVAILABLE
from conftest import registered,crm_contact,tg_update,messages


@pytest.mark.parametrize('raw',['8 (999) 123-45-67','+7 999 123 45 67','9991234567','79991234567','89991234567 доб. 123'])
def test_phone_normalization(raw):
    assert phone(raw) == '+79991234567'


async def test_numeric_start_reserves_without_personal_details(system):
    await system.service.handle('telegram',tg_update('/start 201'),'start')
    claim = await system.db.one('SELECT * FROM lead_claims')
    assert claim['lead_id'] == 201 and claim['method'] == 'numeric_unverified'
    text = (await messages(system))[-1]['text']
    assert 'Иванов' not in text and '7999' not in text
    assert not await system.db.one('SELECT * FROM clients')
    await system.service.handle('telegram',tg_update(callback='segment:active'),'choice')
    sub = await system.db.one('SELECT * FROM subscriptions')
    assert sub['claimed_lead_id'] == 201 and sub['identity_verified'] == 0 and sub['state'] == 'confirmed'
    assert sub['phone'] is None
    note = await system.db.one('SELECT * FROM crm_notes')
    assert 'ID пользователя: 50' in note['text'] and 'не подтверждена' in note['text']


async def test_repeat_start_and_buttons_do_not_duplicate(system):
    for i in range(3):
        await system.service.handle('telegram',tg_update('/start 201'),f'start{i}')
        await system.service.handle('telegram',tg_update(callback='segment:active'),f'choice{i}')
    assert len(await system.db.all('SELECT * FROM clients')) == 1
    assert len(await system.db.all('SELECT * FROM crm_notes')) == 1
    assert len(await system.db.all('SELECT * FROM lead_claims')) == 1


async def test_first_claim_race_two_platforms_and_connections(system):
    first = await system.service.subscription(Incoming('telegram',50,50))
    second = await system.service.subscription(Incoming('max',70,70))
    db2 = Database(system.config.database_path)
    await db2.open()
    other = Service(system.config,db2,system.crm,system.platforms)
    try:
        result = await asyncio.gather(system.service.claim_numeric(first,201),other.claim_numeric(second,201))
        assert sorted(result) == [False,True]
        assert len(await system.db.all('SELECT * FROM lead_claims')) == 1
    finally:
        await db2.close()


async def test_same_account_cannot_claim_many_leads(system):
    system.crm.leads[202] = dict(system.crm.leads[201],id=202)
    sub = await system.service.subscription(Incoming('telegram',50,50))
    assert await system.service.claim_numeric(sub,201)
    fresh = await system.db.one('SELECT * FROM subscriptions WHERE id=?',(sub['id'],))
    assert not await system.service.claim_numeric(fresh,202)
    assert len(await system.db.all('SELECT * FROM lead_claims')) == 1


async def test_invalid_occupied_and_nonexistent_leads_have_same_response(system):
    await system.service.handle('telegram',tg_update('/start 201'),'owner')
    for payload in ['201','999999','not-a-number']:
        await system.service.handle('telegram',tg_update('/start '+payload,uid=70),'denied-'+payload)
        assert (await messages(system))[-1]['text'] == UNAVAILABLE


async def test_persistent_rate_limit_stops_crm_reads(system):
    system.config.lead_start_limit = 2
    for n in range(5):
        await system.service.handle('telegram',tg_update('/start '+str(900+n)),str(n))
    assert system.crm.lead_reads == 2
    assert await system.db.one("SELECT * FROM events WHERE kind='lead_lookup_limited'")


async def test_registration_without_start_parameter_and_skip(system):
    await system.service.handle('telegram',tg_update('/start'),'start')
    await system.service.handle('telegram',tg_update(callback='segment:completed'),'choice')
    await system.service.handle('telegram',tg_update(callback='contact:skip'),'skip')
    sub = await system.db.one('SELECT * FROM subscriptions')
    client = await system.db.one('SELECT * FROM clients')
    assert sub['client_id'] and sub['claimed_lead_id'] is None
    assert client['segment'] == 'completed' and client['contact_id'] is None
    assert len(await system.scheduler.plan()) == 1
    assert not await system.db.one('SELECT * FROM reviews')


async def test_return_after_stop_before_registration_can_subscribe(system):
    await system.service.handle('telegram',tg_update('/stop'),'stop-before-registration')
    await system.service.handle('telegram',tg_update('/start'),'restart')
    await system.service.handle('telegram',tg_update(callback='segment:active'),'group')
    sub = await system.db.one('SELECT * FROM subscriptions')
    assert sub['client_id'] and not sub['stopped']
    assert len(await system.scheduler.plan()) == 1


async def test_phone_unique_and_shared_phone_stays_subscribed(system):
    first = await registered(system)
    assert first['identity_verified'] and first['claimed_lead_id'] == 201
    system.crm.contacts[102] = crm_contact(102,lead_ids=(202,))
    system.crm.leads[202] = dict(system.crm.leads[201],id=202)
    second = await registered(system,uid=60)
    assert second['client_id'] and second['claimed_lead_id'] is None
    assert not await system.db.one('SELECT * FROM reviews')
    assert len(await system.scheduler.plan()) == 2


async def test_missing_phone_then_new_crm_data_auto_matches(system):
    sub = await registered(system,number='+79990000000')
    assert sub['claimed_lead_id'] is None
    system.crm.contacts[101]['custom_fields_values'][0]['values'][0]['value'] = '79990000000'
    await system.service.sync(sub['id'])
    fresh = await system.db.one('SELECT * FROM subscriptions WHERE id=?',(sub['id'],))
    assert fresh['identity_verified'] and fresh['claimed_lead_id'] == 201


async def test_two_verified_platform_accounts_merge(system):
    first = await registered(system)
    second = await registered(system,platform='max',uid=70)
    assert first['client_id'] == second['client_id']
    assert len(await system.db.all('SELECT * FROM clients WHERE merged_into IS NULL')) == 1
    assert len(await system.scheduler.plan()) == 1
    await system.service.handle('max',{'update_type':'bot_started','chat_id':70,'user':{'user_id':70},'payload':'201'},'repeat-max')
    assert 'уже зарегистрированы' in (await messages(system))[-1]['text']


async def test_admin_can_reassign_an_incomplete_reservation_with_audit(system):
    await system.service.handle('telegram',tg_update('/start 201'),'incomplete')
    sub = await registered(system,uid=70,number=None)
    assert await system.service.attach(sub,await system.crm.lead(201),'admin',actor='telegram:admin')
    claim = await system.db.one('SELECT * FROM lead_claims')
    assert claim['subscription_id'] == sub['id']
    assert await system.db.one("SELECT * FROM events WHERE kind='claim_reassigned_by_admin'")


async def test_numeric_unverified_owner_cannot_be_silently_merged(system):
    await system.service.handle('telegram',tg_update('/start 201'),'start')
    await system.service.handle('telegram',tg_update(callback='segment:active'),'choice')
    second = await registered(system,platform='max',uid=70)
    assert second['claimed_lead_id'] is None
    assert len(await system.db.all('SELECT * FROM clients WHERE merged_into IS NULL')) == 2


async def test_numeric_user_with_mismatching_phone_is_not_reassigned(system):
    await system.service.handle('telegram',tg_update('/start 201'),'start')
    await system.service.handle('telegram',tg_update(callback='segment:active'),'choice')
    system.crm.contacts[102] = crm_contact(102,number='79990000000',lead_ids=(202,))
    system.crm.leads[202] = dict(system.crm.leads[201],id=202)
    await system.service.handle('telegram',tg_update(contact={'user_id':50,'phone_number':'79990000000'}),'contact')
    await system.service.sync((await system.db.one('SELECT * FROM subscriptions'))['id'])
    sub = await system.db.one('SELECT * FROM subscriptions')
    assert sub['claimed_lead_id'] == 201 and not sub['identity_verified']
    assert await system.db.one("SELECT * FROM events WHERE kind='phone_lead_mismatch'")


async def test_telegram_foreign_contact_and_max_hmac(system):
    await registered(system,number=None)
    await system.service.handle('telegram',tg_update(contact={'user_id':99,'phone_number':'79991234567'}),'foreign')
    assert (await system.db.one('SELECT * FROM subscriptions'))['phone'] is None
    raw = 'BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Иван\r\nTEL;TYPE=cell:79991234567\r\nEND:VCARD\r\n'
    digest = hmac.new(b'fake-max-token',raw.encode(),hashlib.sha256).hexdigest()
    payload = {'vcf_info':raw,'hash':digest,'max_info':{'user_id':70}}
    assert system.platforms['max'].verify_contact(payload,70) == '+79991234567'
    with pytest.raises(ValueError):
        system.platforms['max'].verify_contact(dict(payload,hash='fake'),70)
    with pytest.raises(ValueError):
        system.platforms['max'].verify_contact(payload,71)
