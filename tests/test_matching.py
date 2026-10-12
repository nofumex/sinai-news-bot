import hashlib
import hmac
import json
import pytest
from sinai.matching import phone, name_score
from sinai.links import issue, token_hash, parse_lead
from conftest import subscriber, registered, crm_contact, tg_update, messages


@pytest.mark.parametrize("raw", ["8 (999) 123-45-67", "+7 999 123 45 67", "9991234567", "79991234567", "89991234567 доб. 123"])
def test_phone_normalization(raw):
    assert phone(raw) == "+79991234567"


@pytest.mark.parametrize("name,threshold", [("Иван Иванович Иванов", 1), ("ИВАНОВ   ИВАН ИВАНОВИЧ", 1),
                                          ("Иванов И. И.", .85), ("Иваноф Иван Иванович", .9)])
def test_fuzzy_names(name, threshold):
    assert name_score(name, "Иванов Иван Иванович") >= threshold


def test_yo_and_single_name_is_not_full_match():
    assert name_score("Фёдоров Пётр", "Федоров Петр") == 1
    assert name_score("Иван", "Иванов Иван Иванович") < .60


async def test_unique_phone_matches_and_note_once(system):
    sub = await registered(system)
    assert sub['state'] == 'confirmed'
    await system.service.sync(sub['id'])
    assert len(await system.db.all("SELECT * FROM crm_notes")) == 1
    assert len(await system.db.all("SELECT * FROM bindings")) == 1
    note = (await system.db.one("SELECT * FROM crm_notes"))['text']
    assert "Telegram" in note and "Контакт CRM: 101" in note


async def test_shared_phone_never_auto_binds(system):
    system.crm.contacts[102] = crm_contact(102, lead_ids=(202,))
    system.crm.leads[202] = dict(system.crm.leads[201], id=202)
    sid = await subscriber(system)
    await system.service.sync(sid)
    assert not await system.db.all("SELECT * FROM clients")
    assert len(json.loads((await system.db.one("SELECT * FROM reviews"))['candidates'])) == 2


async def test_multiple_leads_require_review(system):
    system.crm.contacts[101]['_embedded']['leads'].append({'id': 202})
    system.crm.leads[202] = dict(system.crm.leads[201], id=202)
    sid = await subscriber(system)
    await system.service.sync(sid)
    assert (await system.db.one("SELECT * FROM subscriptions WHERE id=?", (sid,)))['state'] == 'review'


async def test_no_phone_then_name_match_requires_manual(system):
    sid = await subscriber(system, number='+79990000000')
    await system.service.sync(sid)
    assert (await system.db.one("SELECT * FROM subscriptions WHERE id=?", (sid,)))['state'] == 'name'
    await system.db.execute("UPDATE subscriptions SET name='Иванов Иван Иванович' WHERE id=?", (sid,))
    await system.service.sync(sid, by_name=True)
    assert not await system.db.all("SELECT * FROM clients")
    assert (await system.db.one("SELECT * FROM reviews"))['reason'] == 'Совпадение только по ФИО'


async def test_missing_in_crm_creates_not_found_review(system):
    sid = await subscriber(system, number='+79990000000')
    await system.db.execute("UPDATE subscriptions SET name='Смирнова Анна Петровна' WHERE id=?", (sid,))
    await system.service.sync(sid, by_name=True)
    assert (await system.db.one("SELECT * FROM subscriptions WHERE id=?", (sid,)))['state'] == 'not_found'
    assert json.loads((await system.db.one("SELECT * FROM reviews"))['candidates']) == []


async def test_telegram_rejects_foreign_contact(system):
    await system.service.handle('telegram', tg_update('/start'), '1')
    await system.service.handle('telegram', tg_update(callback='segment:active'), '2')
    await system.service.handle('telegram', tg_update(contact={'user_id': 99, 'phone_number': '79991234567'}), '3')
    assert not await system.db.all("SELECT * FROM jobs WHERE kind='sync'")
    await system.service.handle('telegram', tg_update(contact={'user_id': 50, 'phone_number': '79991234567'}), '4')
    assert await system.db.one("SELECT * FROM jobs WHERE kind='sync'")
    sent = await messages(system)
    assert sent[0]['text'].startswith('Добро пожаловать!')
    assert sent[1]['request_contact']


async def test_max_hmac_and_sender(system):
    raw = "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Иван Иванов\r\nTEL;TYPE=cell:79991234567\r\nEND:VCARD\r\n"
    digest = hmac.new(b'fake-max-token', raw.encode(), hashlib.sha256).hexdigest()
    payload = {'vcf_info': raw, 'hash': digest, 'max_info': {'user_id': 50}}
    assert system.platforms['max'].verify_contact(payload, 50) == '+79991234567'
    with pytest.raises(ValueError):
        system.platforms['max'].verify_contact(dict(payload, hash='forged'), 50)
    with pytest.raises(ValueError):
        system.platforms['max'].verify_contact(payload, 51)
    with pytest.raises(ValueError):
        system.platforms['max'].verify_contact({'vcf_info': raw}, 50)


async def test_deeplink_confirms_card_and_consumes_token(system):
    url = await issue(system.db, system.config, 'telegram', 201, 101, 'manager')
    token = url.split('start=')[1]
    await system.service.handle('telegram', tg_update('/start ' + token), 'start-link')
    await system.service.handle('telegram', tg_update(callback='segment:active'), 'segment-link')
    await system.service.handle('telegram', tg_update(contact={'user_id': 50, 'phone_number': '79991234567'}), 'contact-link')
    sub = await system.db.one("SELECT * FROM subscriptions")
    await system.service.sync(sub['id'])
    assert (await system.db.one("SELECT * FROM bindings"))['method'] == 'deeplink_phone'
    assert (await system.db.one("SELECT * FROM links"))['consumed_at']
    await system.service.handle('telegram', tg_update('/start ' + token, uid=99), 'reuse')
    assert 'недействительна' in (await messages(system))[-1]['text']


async def test_deeplink_phone_conflict_manual(system):
    sid = await subscriber(system, number='+79990000000')
    url = await issue(system.db, system.config, 'telegram', 201, 101, 'manager')
    digest = token_hash(url.split('start=')[1])
    await system.db.execute("UPDATE links SET claimed_by=? WHERE token_hash=?", (sid, digest))
    await system.db.execute("UPDATE subscriptions SET deep_token_hash=? WHERE id=?", (digest, sid))
    await system.service.sync(sid)
    assert not await system.db.all("SELECT * FROM clients")
    assert 'Конфликт' in (await system.db.one("SELECT * FROM reviews"))['reason']


async def test_expired_deeplink_rejected(system):
    url = await issue(system.db, system.config, 'telegram', 201, 101, 'manager')
    await system.db.execute("UPDATE links SET expires_at='2000-01-01T00:00:00+00:00'")
    await system.service.handle('telegram', tg_update('/start ' + url.split('start=')[1]), 'expired')
    assert 'истекла' in (await messages(system))[-1]['text']


async def test_client_uniqueness_across_platforms(system):
    await registered(system)
    await registered(system, platform='max', uid=70)
    assert len(await system.db.all("SELECT * FROM clients")) == 1
    assert len(await system.db.all("SELECT * FROM subscriptions")) == 2
    assert len(await system.db.all("SELECT * FROM crm_notes")) == 2


def test_lead_urls_restricted_to_account():
    assert parse_lead('https://sinai24.amocrm.ru/leads/detail/201', 'https://sinai24.amocrm.ru') == 201
    with pytest.raises(ValueError):
        parse_lead('https://evil.example/leads/detail/201', 'https://sinai24.amocrm.ru')


async def test_manager_whitelist_enforced(system):
    await system.service.handle('telegram', tg_update('/link 201'), 'unauthorized')
    assert not await system.db.all("SELECT * FROM links")
    await system.service.handle('telegram', tg_update('/link 201', uid=system.config.admin_id), 'authorized')
    assert len(await system.db.all("SELECT * FROM links")) == 2
