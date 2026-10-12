import json
from unittest.mock import AsyncMock
import pytest
from sinai.content import formatted,capture
from sinai.db import dumps
from sinai.http import AmbiguousSend,RetryLater
from sinai.platforms.base import Incoming
from conftest import registered,tg_update,messages


async def ready_campaign(system,audience='all',parts=1):
    uid = system.config.admin_id
    await system.service.handle('telegram',tg_update(callback='campaign:audience:'+audience,uid=uid),'new-campaign')
    row = await system.db.one("SELECT * FROM campaigns WHERE state='draft'")
    for i in range(parts):
        source = capture('telegram',{'chat':{'id':uid},'message_id':100+i,'text':'Важное сообщение','entities':[{'type':'bold','offset':0,'length':6}]})
        cursor = await system.db.execute('INSERT INTO campaign_items(campaign_id,source_key,content) VALUES(?,?,?)',(row['id'],str(i),dumps(source)))
        for platform in ['telegram','max']:
            payload = {'method':'copy','source':source} if platform == 'telegram' else {'method':'converted','html':source['html'],'assets':[]}
            await system.db.execute('INSERT INTO campaign_variants VALUES(?,?,?)',(cursor.lastrowid,platform,dumps(payload)))
    recipients = await system.service.campaigns.recipients(audience)
    await system.db.execute('UPDATE campaigns SET preview_revision=revision,preview_test_mode=0,preview_recipients=? WHERE id=?',(dumps([(c['id'],s['id']) for c,s in recipients]),row['id']))
    return row['id'],row['revision'],Incoming('telegram',uid,uid)


async def enable(system):
    system.config.test_mode = False
    system.config.broadcasts_enabled = True
    system.service.campaigns.content.send = AsyncMock(return_value='copied-id')


async def test_create_capture_formatting_preview_and_edit_version(system):
    await registered(system)
    uid = system.config.admin_id
    await system.service.handle('telegram',tg_update('/message',uid=uid),'message')
    assert 'Выберите аудиторию' in (await messages(system))[-1]['text']
    await system.service.handle('telegram',tg_update(callback='campaign:audience:all',uid=uid),'audience')
    update = tg_update('Важное сообщение',uid=uid)
    update['message']['entities'] = [{'type':'bold','offset':0,'length':6}]
    await system.service.handle('telegram',update,'content')
    row = await system.db.one('SELECT * FROM campaigns')
    item = await system.db.one('SELECT * FROM campaign_items')
    assert json.loads(item['content'])['html'] == '<b>Важное</b> сообщение'
    await system.service.campaigns.preview(row['id'],row['revision'])
    preview_job = await system.db.one("SELECT * FROM jobs WHERE kind='campaign_preview_send'")
    await system.service.campaigns.preview_send(json.loads(preview_job['payload']))
    await system.db.execute("UPDATE jobs SET state='done' WHERE id=?",(preview_job['id'],))
    ready = await system.db.one("SELECT * FROM jobs WHERE kind='campaign_preview_ready'")
    await system.service.campaigns.preview_ready(json.loads(ready['payload']))
    assert 'Получателей сейчас: 1' in (await messages(system))[-1]['text']
    await system.service.handle('telegram',tg_update(callback=f"campaign:edit:{row['id']}",uid=uid),'edit')
    assert not await system.db.one('SELECT * FROM campaign_items')
    assert (await system.db.one('SELECT * FROM campaigns'))['preview_revision'] is None


async def test_safe_modes_never_launch_clients(system):
    await registered(system)
    cid,revision,admin = await ready_campaign(system)
    await system.service.campaigns.launch(admin,cid,revision)
    assert not await system.db.one('SELECT * FROM campaign_deliveries')
    system.config.test_mode = False
    await system.service.campaigns.launch(admin,cid,revision)
    assert not await system.db.one('SELECT * FROM campaign_deliveries')


async def test_launch_idempotent_and_one_confirmed_client_channel(system):
    await registered(system)
    await registered(system,platform='max',uid=70)
    cid,revision,admin = await ready_campaign(system)
    await enable(system)
    await system.service.campaigns.launch(admin,cid,revision)
    await system.service.campaigns.launch(admin,cid,revision)
    rows = await system.db.all('SELECT * FROM campaign_deliveries')
    assert len(rows) == 1
    await system.service.campaigns.deliver(rows[0]['id'])
    await system.service.campaigns.deliver(rows[0]['id'])
    system.service.campaigns.content.send.assert_awaited_once()
    assert (await system.db.one('SELECT * FROM campaigns'))['state'] == 'completed'


async def test_audience_change_requires_new_preview(system):
    await registered(system)
    cid,revision,admin = await ready_campaign(system)
    await registered(system,uid=70,number=None)
    await enable(system)
    await system.service.campaigns.launch(admin,cid,revision)
    assert not await system.db.one('SELECT * FROM campaign_deliveries')
    assert 'Изменилась аудитория' in (await messages(system))[-1]['text']


async def test_test_preview_not_valid_for_production_send(system):
    await registered(system)
    cid,revision,admin = await ready_campaign(system)
    await system.db.execute('UPDATE campaigns SET preview_test_mode=1')
    await enable(system)
    await system.service.campaigns.launch(admin,cid,revision)
    assert not await system.db.one('SELECT * FROM campaign_deliveries')


async def test_unlinked_clients_receive_campaign_and_completed_audience(system):
    await registered(system,number=None,segment='completed')
    await registered(system,uid=70,number=None,segment='active')
    cid,revision,admin = await ready_campaign(system,'completed')
    await enable(system)
    await system.service.campaigns.launch(admin,cid,revision)
    row = await system.db.one('SELECT * FROM campaign_deliveries')
    await system.service.campaigns.deliver(row['id'])
    system.service.campaigns.content.send.assert_awaited_once()
    assert len(await system.db.all('SELECT * FROM campaign_deliveries')) == 1


async def test_stop_after_launch_skips_recipient(system):
    await registered(system)
    cid,revision,admin = await ready_campaign(system)
    await enable(system)
    await system.service.campaigns.launch(admin,cid,revision)
    await system.service.handle('telegram',tg_update('/stop'),'stop')
    row = await system.db.one('SELECT * FROM campaign_deliveries')
    await system.service.campaigns.deliver(row['id'])
    system.service.campaigns.content.send.assert_not_awaited()
    assert (await system.db.one('SELECT * FROM campaign_deliveries'))['state'] == 'unsubscribed'


async def test_retry_resumes_only_unsent_parts(system):
    await registered(system)
    cid,revision,admin = await ready_campaign(system,parts=2)
    await enable(system)
    await system.service.campaigns.launch(admin,cid,revision)
    row = await system.db.one('SELECT * FROM campaign_deliveries')
    system.service.campaigns.content.send.side_effect = ['first',RetryLater('max',15)]
    with pytest.raises(RetryLater):
        await system.service.campaigns.deliver(row['id'])
    states = [r['state'] for r in await system.db.all('SELECT * FROM campaign_parts ORDER BY item_id')]
    assert states == ['sent','pending']
    system.service.campaigns.content.send.side_effect = None
    await system.service.campaigns.deliver(row['id'])
    assert system.service.campaigns.content.send.await_count == 3


async def test_unknown_and_restart_are_never_repeated(system):
    await registered(system)
    cid,revision,admin = await ready_campaign(system)
    await enable(system)
    await system.service.campaigns.launch(admin,cid,revision)
    row = await system.db.one('SELECT * FROM campaign_deliveries')
    system.service.campaigns.content.send.side_effect = AmbiguousSend('telegram',503)
    with pytest.raises(AmbiguousSend):
        await system.service.campaigns.deliver(row['id'])
    await system.worker.recover()
    await system.service.campaigns.deliver(row['id'])
    system.service.campaigns.content.send.assert_awaited_once()
    assert (await system.db.one('SELECT * FROM campaign_parts'))['state'] == 'unknown'


async def test_merge_after_queue_deduplicates_campaign(system):
    first = await registered(system)
    second = await registered(system,platform='max',uid=70,number=None)
    cid,revision,admin = await ready_campaign(system)
    await enable(system)
    await system.service.campaigns.launch(admin,cid,revision)
    rows = await system.db.all('SELECT * FROM campaign_deliveries ORDER BY id')
    assert len(rows) == 2
    await system.db.execute('UPDATE subscriptions SET phone=? WHERE id=?',('+79991234567',second['id']))
    await system.service.sync(second['id'])
    assert (await system.db.one('SELECT * FROM subscriptions WHERE id=?',(second['id'],)))['client_id'] == first['client_id']
    for row in rows:
        await system.service.campaigns.deliver(row['id'])
    system.service.campaigns.content.send.assert_awaited_once()
    assert [r['state'] for r in await system.db.all('SELECT * FROM campaign_deliveries ORDER BY id')] == ['sent','duplicate']


async def test_album_is_one_item_and_keeps_all_native_ids(system):
    uid = system.config.admin_id
    await system.service.handle('telegram',tg_update(callback='campaign:audience:all',uid=uid),'album-start')
    for i in [100,101]:
        update = tg_update(uid=uid,update_id=i)
        update['message'].update(message_id=i,media_group_id='album',photo=[{'file_id':str(i)}])
        await system.service.handle('telegram',update,str(i))
    rows = await system.db.all('SELECT * FROM campaign_items')
    assert len(rows) == 1
    assert json.loads(rows[0]['content'])['message_ids'] == [100,101]


def test_utf16_formatting_with_emoji_nested_bold_links():
    text = '😀 ссылка'
    entities = [{'type':'bold','offset':3,'length':6},{'type':'text_link','offset':3,'length':6,'url':'https://example.com?a=1&b=2'}]
    result = formatted(text,entities,'telegram')
    assert result.startswith('😀 ') and '<b>' in result and '<a href=' in result
    assert '&amp;b=2' in result and 'ссылка' in result


async def test_unsupported_cross_platform_attachment_blocks_preview(system):
    source = capture('telegram',{'chat':{'id':1},'message_id':1,'sticker':{'file_id':'s'}})
    assert (await system.service.campaigns.content.prepare(source,'telegram'))['method'] == 'copy'
    with pytest.raises(ValueError):
        await system.service.campaigns.content.prepare(source,'max')


async def test_native_preview_freezes_source_to_bot_message(system):
    await registered(system)
    cid,revision,_ = await ready_campaign(system)
    system.config.test_mode = False
    system.service.campaigns.content.send = AsyncMock(return_value='900')
    await system.service.campaigns.preview_send({'campaign_id':cid,'revision':revision,'platform':'telegram','user_id':system.config.admin_id,'chat_id':system.config.admin_id})
    variant = await system.db.one("SELECT payload FROM campaign_variants WHERE platform='telegram'")
    assert json.loads(variant['payload'])['source']['message_id'] == 900


async def test_late_album_part_invalidates_prepared_variant(system):
    uid = system.config.admin_id
    await system.service.handle('telegram',tg_update(callback='campaign:audience:all',uid=uid),'album-start')
    def part(mid):
        update = tg_update(uid=uid,update_id=mid)
        update['message'].update(message_id=mid,media_group_id='late',photo=[{'file_id':str(mid)}])
        return update
    await system.service.handle('telegram',part(100),'first')
    item = await system.db.one('SELECT * FROM campaign_items')
    await system.db.execute('INSERT INTO campaign_variants VALUES(?,?,?)',(item['id'],'telegram',dumps({'method':'copy','source':json.loads(item['content'])})))
    await system.service.handle('telegram',part(101),'late')
    assert not await system.db.one('SELECT * FROM campaign_variants')
    assert json.loads((await system.db.one('SELECT * FROM campaign_items'))['content'])['message_ids'] == [100,101]


async def test_restarting_between_parts_resumes_pending_part(system):
    await registered(system)
    cid,revision,admin = await ready_campaign(system,parts=2)
    await enable(system)
    await system.service.campaigns.launch(admin,cid,revision)
    parts = await system.db.all('SELECT * FROM campaign_parts ORDER BY item_id')
    await system.db.execute("UPDATE campaign_parts SET state='sent',external_id='old' WHERE item_id=?",(parts[0]['item_id'],))
    await system.db.execute("UPDATE campaign_deliveries SET state='retry'")
    await system.worker.recover()
    await system.service.campaigns.deliver(parts[0]['delivery_id'])
    system.service.campaigns.content.send.assert_awaited_once()


async def test_restarting_during_campaign_send_marks_unknown(system):
    await registered(system)
    cid,revision,admin = await ready_campaign(system)
    await enable(system)
    await system.service.campaigns.launch(admin,cid,revision)
    await system.db.execute("UPDATE campaign_deliveries SET state='sending'")
    await system.db.execute("UPDATE campaign_parts SET state='sending'")
    await system.worker.recover()
    row = await system.db.one('SELECT * FROM campaign_deliveries')
    assert row['state'] == 'unknown'
    await system.service.campaigns.deliver(row['id'])
    system.service.campaigns.content.send.assert_not_awaited()
