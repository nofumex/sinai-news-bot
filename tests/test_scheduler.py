from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
import pytest
from sinai.db import now
from sinai.http import APIError, AmbiguousSend, RetryLater
from sinai.scheduler import anniversary, latest_cycle, ACTIVE_TEXT, COMPLETED_TEXT, TZ
from conftest import registered, tg_update


def test_calendar_months_do_not_drift():
    anchor = datetime(2026, 1, 31, 10, 45, tzinfo=TZ)
    assert anniversary(anchor, 1).day == 28
    assert anniversary(anchor, 2).day == 31
    assert anniversary(datetime(2024, 1, 31, tzinfo=TZ), 1).day == 29


@pytest.mark.parametrize('interval', [1, 2])
def test_first_send_and_timezone_boundary(interval):
    anchor = datetime(2026, 1, 31, 10, 45, tzinfo=TZ)
    first = anniversary(anchor, interval)
    assert latest_cycle(anchor, first - timedelta(seconds=1), interval) == 0
    assert latest_cycle(anchor, first.astimezone(timezone.utc), interval) == interval
    assert latest_cycle(anchor, anniversary(anchor, interval * 4), interval) == interval * 4


async def prepare(system, months=4):
    from dateutil.relativedelta import relativedelta
    sub = await registered(system, at=now() - relativedelta(months=months, minutes=10))
    system.config.test_mode = False
    system.config.broadcasts_enabled = True
    system.platforms['telegram'].send = AsyncMock(return_value='message-123')
    return sub


async def test_safe_defaults_do_not_consume_delivery_cycles(system):
    from dateutil.relativedelta import relativedelta
    await registered(system, at=now() - relativedelta(months=6))
    await system.scheduler.tick()
    assert not await system.db.all("SELECT * FROM deliveries")
    assert (await system.scheduler.plan())[0]['due']
    system.config.test_mode = False
    await system.scheduler.tick()
    assert not await system.db.all("SELECT * FROM deliveries")


async def test_no_catchup_batch_and_unique_send(system):
    await prepare(system, months=8)
    await system.scheduler.tick()
    await system.scheduler.tick()
    rows = await system.db.all("SELECT * FROM deliveries")
    assert len(rows) == 1 and rows[0]['cycle'] == 8
    await system.scheduler.deliver(rows[0]['id'])
    await system.scheduler.deliver(rows[0]['id'])
    system.platforms['telegram'].send.assert_awaited_once()
    assert system.platforms['telegram'].send.await_args.args[2] == ACTIVE_TEXT


async def test_one_channel_for_two_accounts(system):
    await prepare(system)
    await registered(system, platform='max', uid=70)
    system.platforms['max'].send = AsyncMock(return_value='max-123')
    await system.scheduler.tick()
    row = await system.db.one("SELECT * FROM deliveries")
    await system.scheduler.deliver(row['id'])
    system.platforms['telegram'].send.assert_awaited_once()
    system.platforms['max'].send.assert_not_awaited()


async def test_completed_initial_and_changed_segment_sticky(system):
    system.crm.leads[201]['pipeline_id'] = 8993570
    sub = await prepare(system)
    client = await system.db.one("SELECT * FROM clients")
    assert client['segment'] == 'completed'
    anchor = client['subscribed_at']
    system.crm.leads[201]['pipeline_id'] = 1312204
    await system.service.reconcile_client(client['id'])
    assert (await system.db.one("SELECT * FROM clients"))['segment'] == 'completed'
    assert (await system.db.one("SELECT * FROM clients"))['subscribed_at'] == anchor
    assert sub['selected_segment'] == 'active'


async def test_fresh_crm_check_changes_text_before_send(system):
    await prepare(system)
    await system.scheduler.tick()
    system.crm.leads[201]['pipeline_id'] = 8993570
    row = await system.db.one("SELECT * FROM deliveries")
    await system.scheduler.deliver(row['id'])
    assert system.platforms['telegram'].send.await_args.args[2] == COMPLETED_TEXT
    assert system.crm.lead_reads > 1


async def test_completed_on_other_linked_lead(system):
    system.crm.contacts[101]['_embedded']['leads'].append({'id': 202})
    system.crm.leads[202] = dict(system.crm.leads[201], id=202, pipeline_id=8993570)
    from conftest import subscriber
    sid = await subscriber(system)
    sub = await system.db.one("SELECT * FROM subscriptions WHERE id=?", (sid,))
    await system.service.bind(sub, {'contact_id': 101, 'lead_id': 201}, 'manual', 'admin')
    assert (await system.db.one("SELECT * FROM clients"))['segment'] == 'completed'


async def test_stop_cancels_queued_send_across_channels(system):
    await prepare(system)
    await registered(system, platform='max', uid=70)
    await system.scheduler.tick()
    await system.service.handle('telegram', tg_update('/stop'), 'stop')
    row = await system.db.one("SELECT * FROM deliveries")
    await system.scheduler.deliver(row['id'])
    assert (await system.db.one("SELECT * FROM deliveries"))['state'] == 'skipped'
    assert all(x['stopped'] for x in await system.db.all("SELECT * FROM subscriptions"))
    system.platforms['telegram'].send.assert_not_awaited()


async def test_resume_preserves_anchor(system):
    await prepare(system)
    anchor = (await system.db.one("SELECT * FROM clients"))['subscribed_at']
    await system.service.handle('telegram', tg_update('/stop'), 'stop')
    await system.service.handle('telegram', tg_update('/start'), 'resume')
    client = await system.db.one("SELECT * FROM clients")
    assert client['subscribed_at'] == anchor and client['opted_out'] == 0


async def test_stale_retry_is_skipped(system):
    await prepare(system)
    await system.scheduler.tick()
    row = await system.db.one("SELECT * FROM deliveries")
    await system.db.execute("UPDATE deliveries SET cycle=cycle-1 WHERE id=?", (row['id'],))
    await system.scheduler.deliver(row['id'])
    assert (await system.db.one("SELECT * FROM deliveries"))['state'] == 'skipped'
    system.platforms['telegram'].send.assert_not_awaited()


async def test_429_is_retryable(system):
    await prepare(system)
    await system.scheduler.tick()
    system.platforms['telegram'].send.side_effect = RetryLater('telegram', 60)
    row = await system.db.one("SELECT * FROM deliveries")
    with pytest.raises(RetryLater):
        await system.scheduler.deliver(row['id'])
    assert (await system.db.one("SELECT * FROM deliveries"))['state'] == 'retry'
    system.platforms['telegram'].send.side_effect = None
    await system.scheduler.deliver(row['id'])
    assert (await system.db.one("SELECT * FROM deliveries"))['state'] == 'sent'


async def test_unknown_outcome_never_repeats(system):
    await prepare(system)
    await system.scheduler.tick()
    system.platforms['telegram'].send.side_effect = AmbiguousSend('telegram', 503)
    row = await system.db.one("SELECT * FROM deliveries")
    with pytest.raises(AmbiguousSend):
        await system.scheduler.deliver(row['id'])
    await system.scheduler.deliver(row['id'])
    system.platforms['telegram'].send.assert_awaited_once()
    assert (await system.db.one("SELECT * FROM deliveries"))['state'] == 'unknown'


async def test_blocked_channel_falls_back(system):
    await prepare(system)
    await registered(system, platform='max', uid=70)
    system.platforms['telegram'].send.side_effect = APIError('telegram', 403)
    system.platforms['max'].send = AsyncMock(return_value='max-321')
    await system.scheduler.tick()
    row = await system.db.one("SELECT * FROM deliveries")
    with pytest.raises(RetryLater):
        await system.scheduler.deliver(row['id'])
    await system.scheduler.deliver(row['id'])
    system.platforms['telegram'].send.assert_awaited_once()
    system.platforms['max'].send.assert_awaited_once()


async def test_changed_phone_pauses_broadcast(system):
    await prepare(system)
    await system.scheduler.tick()
    system.crm.contacts[101]['custom_fields_values'][0]['values'][0]['value'] = '79990000000'
    await system.scheduler.deliver((await system.db.one("SELECT * FROM deliveries"))['id'])
    system.platforms['telegram'].send.assert_not_awaited()
    assert await system.db.one("SELECT * FROM reviews WHERE status='pending'")


async def test_recovery_marks_sending_unknown(system):
    await prepare(system)
    await system.scheduler.tick()
    await system.db.execute("UPDATE deliveries SET state='sending'")
    await system.worker.recover()
    assert (await system.db.one("SELECT * FROM deliveries"))['state'] == 'unknown'


async def test_intervals_are_independent(system):
    await system.db.set_setting('active_interval_months', 2)
    assert await system.scheduler.interval('active') == 2
    assert await system.scheduler.interval('completed') == 1
