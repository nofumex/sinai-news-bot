from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from .db import now, stamp, dumps
from .http import APIError
from .links import parse_lead
from .matching import contact_phones
from .platforms.base import Incoming, button

WELCOME = 'Добро пожаловать! Это официальный чат-бот компании „Синай“. Здесь мы будем отправлять вам особенно важные новости'
UNAVAILABLE = 'Ссылка недоступна. Откройте /start без параметра, чтобы подписаться без привязки к CRM.'


class Service:
    def __init__(self, config, db, crm, platforms):
        self.config, self.db, self.crm, self.platforms = config, db, crm, platforms
        self.context, self.sequence = '', 0
        self.admin = self.campaigns = None

    async def say(self, incoming, text, buttons=None, **kwargs):
        self.sequence += 1
        await self.db.enqueue(f'reply:{self.context}:{self.sequence}', 'message',
                              {'platform': incoming.platform, 'user_id': incoming.user_id, 'chat_id': incoming.chat_id,
                               'text': text, 'buttons': buttons, **kwargs})

    async def tell_sub(self, sub, text):
        await self.say(Incoming(sub['platform'], sub['user_id'], sub['chat_id']), text)

    async def subscription(self, incoming):
        await self.db.execute('INSERT INTO subscriptions(platform,user_id,chat_id,created_at) VALUES(?,?,?,?) '
                              'ON CONFLICT(platform,user_id) DO UPDATE SET chat_id=excluded.chat_id',
                              (incoming.platform, incoming.user_id, incoming.chat_id, stamp()))
        return await self.db.one('SELECT * FROM subscriptions WHERE platform=? AND user_id=?', (incoming.platform, incoming.user_id))

    async def allow_lead_lookup(self, sub):
        async with self.db.transaction():
            allowed = True
            for key, limit in [(f"lead:{sub['platform']}:{sub['user_id']}", self.config.lead_start_limit),
                               ('lead:global', self.config.lead_start_global_limit)]:
                row = await self.db.one('SELECT * FROM rate_windows WHERE key=?', (key,))
                if not row or datetime.fromisoformat(row['started_at']) < now()-timedelta(seconds=self.config.lead_start_window_seconds):
                    await self.db.execute('INSERT INTO rate_windows VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET started_at=excluded.started_at,count=1', (key, stamp()))
                else:
                    await self.db.execute('UPDATE rate_windows SET count=count+1 WHERE key=?', (key,))
                    allowed = allowed and row['count'] < limit
            if not allowed:
                await self.db.event('lead_lookup_limited', sub['id'])
            return allowed

    async def claim_numeric(self, sub, lead_id):
        existing = await self.db.one('SELECT * FROM lead_claims WHERE lead_id=?', (lead_id,))
        if existing and existing['subscription_id'] == sub['id']:
            return True
        if existing and sub['claimed_lead_id'] == lead_id and sub['client_id'] and existing['client_id']:
            if (await self.db.canonical(sub['client_id']))['id'] == (await self.db.canonical(existing['client_id']))['id']:
                return True  # Already linked through a verified phone/admin merge, no new occupation.
        if sub['claimed_lead_id'] or (sub['client_id'] and sub['crm_method']):
            return False
        if not await self.allow_lead_lookup(sub):
            return False
        try:
            lead = await self.crm.lead(lead_id)
        except APIError as exc:
            if exc.status == 404:
                return False
            raise
        if lead.get('id') != lead_id or lead.get('is_deleted'):
            return False
        async with self.db.transaction():
            await self.db.execute("INSERT OR IGNORE INTO lead_claims VALUES(?,?,?,'numeric_unverified',?,?,?,?)",
                                  (lead_id, sub['id'], sub['client_id'], lead['pipeline_id'], lead['status_id'], stamp(), stamp()))
            owner = await self.db.one('SELECT * FROM lead_claims WHERE lead_id=?', (lead_id,))
            if owner is None or owner['subscription_id'] != sub['id']:
                await self.db.event('lead_claim_conflict', sub['id'], detail={'lead_id': lead_id})
                return False
            await self.db.execute("UPDATE subscriptions SET claimed_lead_id=?,crm_method='numeric_unverified',identity_verified=0 WHERE id=?", (lead_id, sub['id']))
            await self.cache_lead(lead)
            await self.db.event('lead_claimed', sub['id'], detail={'lead_id': lead_id, 'identity_verified': False})
        return True

    async def handle(self, platform, update, context):
        self.context, self.sequence = context, 0
        incoming = self.platforms[platform].parse(update)
        if not incoming:
            return
        if incoming.callback_id:
            try:
                await self.platforms[platform].answer(incoming.callback_id)
            except APIError:
                pass
        if incoming.text == '/message' or incoming.callback.startswith('campaign:'):
            if self.config.admin(platform, incoming.user_id):
                await self.campaigns.handle(incoming)
            else:
                await self.say(incoming, 'Доступ запрещён.')
            return
        if incoming.text == '/admin' or incoming.callback.startswith('admin:'):
            if self.config.admin(platform, incoming.user_id):
                await self.admin.handle(incoming)
            else:
                await self.say(incoming, 'Доступ запрещён.')
            return
        if self.config.admin(platform, incoming.user_id):
            if await self.admin.consume(incoming) or await self.campaigns.consume(incoming):
                return
        if incoming.text.startswith('/link'):
            if not self.config.manager(platform, incoming.user_id):
                await self.say(incoming, 'Доступ запрещён.')
                return
            try:
                _, value = incoming.text.split()
                lid = parse_lead(value, self.config.amocrm_base_url)
                await self.crm.lead(lid)
                urls = [f'https://t.me/{self.config.tg_bot_username}?start={lid}']
                if self.config.max_bot_username:
                    urls.append(f'https://max.ru/{self.config.max_bot_username}?start={lid}')
                await self.say(incoming, 'Ссылки занимают сделку первым открывшим аккаунтом; личность не проверяется.\n'+'\n'.join(urls))
            except (ValueError, APIError):
                await self.say(incoming, 'Укажите /link ID или URL существующей сделки.')
            return
        sub = await self.subscription(incoming)
        if incoming.stopped or incoming.text.strip() == '/stop':
            await self.opt_out(sub)
            if not incoming.stopped:
                await self.say(incoming, 'Вы отписались во всех подключённых каналах. Для возврата — /start.', remove_keyboard=True)
            return
        if incoming.text.strip().split(maxsplit=1)[0:1] == ['/start']:
            parts = incoming.text.strip().split(maxsplit=1)
            if len(parts) == 2:
                value = parts[1]
                if not value.isascii() or not value.isdigit() or not 0 < int(value) < 2**63:
                    await self.say(incoming, UNAVAILABLE)
                    return
                if not await self.claim_numeric(sub, int(value)):
                    await self.say(incoming, UNAVAILABLE)
                    return
                sub = await self.db.one('SELECT * FROM subscriptions WHERE id=?', (sub['id'],))
            if sub['client_id']:
                async with self.db.transaction():
                    client = await self.db.canonical(sub['client_id'])
                    await self.db.execute('UPDATE subscriptions SET stopped=0,blocked=0 WHERE id=?', (sub['id'],))
                    await self.db.execute('UPDATE clients SET opted_out=0 WHERE id=?', (client['id'],))
                    await self.db.event('resubscribe', sub['id'], client['id'])
                if sub['claimed_lead_id'] and sub['state'] != 'confirmed':
                    await self.attach(sub, await self.crm.lead(sub['claimed_lead_id']), 'numeric_unverified')
                await self.say(incoming, 'Вы уже зарегистрированы. Подписка активна; /stop — отписка, /channel — выбор канала.')
                return
            await self.say(incoming, WELCOME, [[button('Я действующий клиент', 'segment:active')], [button('Моя процедура уже завершена', 'segment:completed')]])
            return
        if incoming.callback in {'segment:active', 'segment:completed'}:
            if sub['client_id']:
                if sub['claimed_lead_id'] and sub['state'] != 'confirmed':
                    await self.attach(sub, await self.crm.lead(sub['claimed_lead_id']), 'numeric_unverified')
                await self.say(incoming, 'Вы уже зарегистрированы. Дата подписки сохранена.')
                return
            await self.db.execute("UPDATE subscriptions SET selected_segment=?,state='contact',subscribed_at=COALESCE(subscribed_at,?) WHERE id=?", (incoming.callback.split(':')[1], stamp(), sub['id']))
            sub = await self.db.one('SELECT * FROM subscriptions WHERE id=?', (sub['id'],))
            await self.register_local(sub)
            if sub['claimed_lead_id']:
                await self.attach(sub, await self.crm.lead(sub['claimed_lead_id']), 'numeric_unverified')
                await self.say(incoming, 'Регистрация завершена. Вы подписаны на новости «Синай». /stop — отписка.')
            else:
                await self.say(incoming, 'Вы подписаны. Можно поделиться своим контактом для связи с CRM или пропустить этот шаг.', request_contact=True)
                await self.say(incoming, 'Передача контакта необязательна.', [[button('Пропустить', 'contact:skip')]])
            return
        if incoming.callback == 'contact:skip':
            if sub['client_id']:
                await self.say(incoming, 'Регистрация завершена без передачи контакта. Вы будете получать рассылки выбранной группы.', remove_keyboard=True)
            return
        if incoming.text.strip() == '/channel' and sub['client_id']:
            client = await self.db.canonical(sub['client_id'])
            await self.db.execute('UPDATE clients SET preferred_platform=? WHERE id=?', (platform, client['id']))
            await self.say(incoming, 'Этот мессенджер выбран для рассылок.')
            return
        if incoming.contact:
            if not sub['client_id']:
                await self.say(incoming, 'Сначала выберите группу: /start.')
                return
            try:
                number = self.platforms[platform].verify_contact(incoming.contact, incoming.user_id)
            except (ValueError, KeyError, TypeError):
                await self.say(incoming, 'Контакт не подтверждён. Используйте кнопку отправки своего контакта или пропустите шаг.', request_contact=True)
                return
            await self.db.execute('UPDATE subscriptions SET phone=? WHERE id=?', (number, sub['id']))
            await self.db.event('contact_verified', sub['id'], sub['client_id'], {'method': platform+'_contact'})
            await self.db.enqueue(f"sync:{context}:{sub['id']}", 'sync', {'subscription_id': sub['id']})
            await self.say(incoming, 'Контакт принят. Подписка действует независимо от результата проверки CRM.', remove_keyboard=True)
            return
        await self.say(incoming, 'Подписка — /start. Отписка — /stop. Канал рассылок — /channel.')

    async def opt_out(self, sub, *, in_transaction=False):
        async def update():
            await self.db.execute('UPDATE subscriptions SET stopped=1 WHERE id=?', (sub['id'],))
            if sub['client_id']:
                client = await self.db.canonical(sub['client_id'])
                await self.db.execute('UPDATE clients SET opted_out=1 WHERE id=?', (client['id'],))
                await self.db.execute('UPDATE subscriptions SET stopped=1 WHERE client_id=?', (client['id'],))
            await self.db.event('unsubscribe', sub['id'], sub['client_id'])
        if in_transaction:
            await update()
        else:
            async with self.db.transaction():
                await update()

    async def register_local(self, sub):
        async with self.db.transaction():
            current = await self.db.one('SELECT * FROM subscriptions WHERE id=?', (sub['id'],))
            if current['client_id']:
                return current['client_id']
            cursor = await self.db.execute("INSERT INTO clients(name,segment,subscribed_at,preferred_platform,segment_source,created_at,local_subscription_id) VALUES('',?,?,?,'selection',?,?)",
                                           (sub['selected_segment'] or 'active', sub['subscribed_at'] or stamp(), self.config.preferred_platform, stamp(), sub['id']))
            cid = cursor.lastrowid
            await self.db.execute("UPDATE subscriptions SET client_id=?,state='registered',stopped=0,blocked=0,subscribed_at=COALESCE(subscribed_at,?) WHERE id=?", (cid, stamp(), sub['id']))
            await self.db.event('registered', sub['id'], cid)
            return cid

    async def merge(self, source, target):
        if source['id'] == target['id']:
            return target['id']
        cid = target['id']
        await self.db.execute('UPDATE subscriptions SET client_id=? WHERE client_id=?', (cid, source['id']))
        await self.db.execute('UPDATE lead_claims SET client_id=? WHERE client_id=?', (cid, source['id']))
        await self.db.execute('UPDATE clients SET merged_into=? WHERE id=?', (cid, source['id']))
        await self.db.execute('UPDATE clients SET subscribed_at=?,opted_out=? WHERE id=?', (min(source['subscribed_at'], target['subscribed_at']), int(bool(source['opted_out'] or target['opted_out'])), cid))
        if source['opted_out'] or target['opted_out']:
            await self.db.execute('UPDATE subscriptions SET stopped=1 WHERE client_id=?', (cid,))
        await self.db.event('clients_merged', client_id=cid, detail={'source_id': source['id']})
        return cid

    async def attach(self, sub, lead, method, contact=None, actor=None):
        verified = method == 'phone'
        actual_leads = await self.crm.leads_for(contact) if verified else [lead]
        cid = await self.register_local(sub)
        async with self.db.transaction():
            sub = await self.db.one('SELECT * FROM subscriptions WHERE id=?', (sub['id'],))
            if sub['claimed_lead_id'] and sub['claimed_lead_id'] != lead['id']:
                return False
            source = await self.db.canonical(sub['client_id'] or cid)
            owner = await self.db.one('SELECT * FROM lead_claims WHERE lead_id=?', (lead['id'],))
            existing = await self.db.one('SELECT * FROM clients WHERE contact_id=? AND merged_into IS NULL AND identity_verified=1', (contact['id'],)) if verified else None
            if owner and owner['subscription_id'] != sub['id']:
                owner_client = await self.db.canonical(owner['client_id']) if owner['client_id'] else None
                permitted = method == 'admin' or (verified and owner_client and owner_client['identity_verified'] and owner_client['contact_id'] == contact['id'])
                if not permitted:
                    await self.db.event('sync_conflict', sub['id'], source['id'], {'lead_id': lead['id']})
                    return False
                if not owner_client and method == 'admin':
                    await self.db.execute('UPDATE subscriptions SET claimed_lead_id=NULL,crm_method=NULL WHERE id=?',(owner['subscription_id'],))
                    await self.db.execute('UPDATE lead_claims SET subscription_id=?,client_id=?,method=? WHERE lead_id=?',(sub['id'],source['id'],method,lead['id']))
                    await self.db.event('claim_reassigned_by_admin',sub['id'],source['id'],{'lead_id':lead['id'],'previous_subscription_id':owner['subscription_id'],'actor':actor})
                    owner_client = source
                if not owner_client:
                    return False
                existing = owner_client or existing
            target = existing or source
            if verified:
                canonical_contact = await self.db.one('SELECT * FROM clients WHERE contact_id=? AND merged_into IS NULL', (contact['id'],))
                if canonical_contact and canonical_contact['id'] != target['id']:
                    if not canonical_contact['identity_verified']:
                        return False
                    target = canonical_contact
            cid = await self.merge(source, target)
            if not owner:
                await self.db.execute('INSERT INTO lead_claims VALUES(?,?,?,?,?,?,?,?)', (lead['id'], sub['id'], cid, method, lead['pipeline_id'], lead['status_id'], stamp(), stamp()))
            else:
                await self.db.execute('UPDATE lead_claims SET client_id=?,pipeline_id=?,status_id=?,checked_at=? WHERE lead_id=?', (cid, lead['pipeline_id'], lead['status_id'], stamp(), lead['id']))
            segment = 'completed' if any(x['pipeline_id'] == self.config.completed_pipeline_id for x in actual_leads) else 'active'
            await self.db.execute("UPDATE clients SET segment=?,segment_source='crm',last_crm_check=?,identity_verified=MAX(identity_verified,?) WHERE id=?", (segment, stamp(), int(verified), cid))
            if verified:
                await self.db.execute('UPDATE clients SET contact_id=?,name=? WHERE id=?', (contact['id'], contact.get('name',''), cid))
                await self.db.execute("INSERT INTO bindings(subscription_id,contact_id,lead_id,method,verified_by,created_at,crm_phones) VALUES(?,?,?,?,?,?,?) ON CONFLICT(subscription_id,contact_id,lead_id) DO UPDATE SET method=excluded.method,crm_phones=excluded.crm_phones", (sub['id'], contact['id'], lead['id'], method, actor, stamp(), dumps(sorted(contact_phones(contact)))))
            await self.db.execute("UPDATE subscriptions SET client_id=?,claimed_lead_id=?,crm_method=?,identity_verified=MAX(identity_verified,?),state='confirmed' WHERE id=?", (cid, lead['id'], method, int(verified), sub['id']))
            await self.cache_lead(lead)
            key = f"subscription-{sub['id']}-lead-{lead['id']}"
            old = await self.db.one('SELECT 1 FROM crm_notes WHERE lead_id=? AND key LIKE ?', (lead['id'], f"subscription-{sub['id']}-%"))
            if not old:
                date = datetime.fromisoformat(sub['subscribed_at']).astimezone(ZoneInfo('Asia/Krasnoyarsk')).strftime('%d.%m.%Y %H:%M')
                text = f"Клиент подписался на официальный бот Синай | {sub['platform']} | ID пользователя: {sub['user_id']} | {date} | {'Действующий клиент' if segment=='active' else 'Процедура завершена'} | Метод: {method} | Личность: {'подтверждена телефоном' if verified else 'не подтверждена'}"
                await self.db.execute('INSERT OR IGNORE INTO crm_notes(key,lead_id,text,created_at) VALUES(?,?,?,?)', (key, lead['id'], text, stamp()))
                await self.db.enqueue('note:'+key, 'note', {'key': key})
                await self.db.event('matched_auto' if verified else 'matched_unverified', sub['id'], cid, {'method': method, 'lead_id': lead['id'], 'actor': actor})
        return True

    async def sync(self, sub_id, **_legacy):
        sub = await self.db.one('SELECT * FROM subscriptions WHERE id=?', (sub_id,))
        if not sub or sub['stopped'] or not sub['phone'] or not sub['client_id'] or sub['identity_verified']:
            return
        pairs = []
        for item in await self.crm.by_phone(sub['phone']):
            contact = await self.crm.contact(item['id'])
            if sub['phone'] in contact_phones(contact):
                pairs.extend((contact, lead) for lead in await self.crm.leads_for(contact))
        if len(pairs) == 1:
            contact, lead = pairs[0]
            if sub['claimed_lead_id'] and sub['claimed_lead_id'] != lead['id']:
                await self.db.event('phone_lead_mismatch', sub['id'], sub['client_id'])
                return
            await self.attach(sub, lead, 'phone', contact)
        else:
            await self.db.event('phone_unmatched' if not pairs else 'phone_ambiguous', sub['id'], sub['client_id'])

    async def cache_lead(self, lead):
        await self.db.execute('INSERT INTO leads VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET pipeline_id=excluded.pipeline_id,status_id=excluded.status_id,checked_at=excluded.checked_at', (lead['id'], lead['pipeline_id'], lead['status_id'], stamp()))

    async def reconcile_client(self, client_id):
        client = await self.db.canonical(client_id)
        if not client:
            return
        subs = await self.db.all('SELECT * FROM subscriptions WHERE client_id=? AND claimed_lead_id IS NOT NULL', (client['id'],))
        if not subs:
            return
        leads = [await self.crm.lead(lid) for lid in sorted({s['claimed_lead_id'] for s in subs})]
        if client['identity_verified'] and client['contact_id']:
            leads.extend(await self.crm.leads_for(await self.crm.contact(client['contact_id'])))
        segment = 'completed' if any(x['pipeline_id'] == self.config.completed_pipeline_id for x in leads) else 'active'
        async with self.db.transaction():
            for lead in leads:
                await self.cache_lead(lead)
            await self.db.execute("UPDATE clients SET segment=?,segment_source='crm',last_crm_check=? WHERE id=?", (segment, stamp(), client['id']))
            if client['segment'] != segment:
                await self.db.event('segment_'+segment, client_id=client['id'])

    async def reconcile_lead(self, lead_id):
        for row in await self.db.all('SELECT DISTINCT client_id FROM subscriptions WHERE claimed_lead_id=? AND client_id IS NOT NULL', (lead_id,)):
            await self.reconcile_client(row['client_id'])
