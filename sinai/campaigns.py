import json
from datetime import timedelta
from .content import Content, capture
from .db import dumps, now, stamp
from .http import APIError, AmbiguousSend, RetryLater
from .platforms.base import Incoming, button


class Campaigns:
    def __init__(self, config, db, service, scheduler):
        self.config, self.db, self.service, self.scheduler = config, db, service, scheduler
        self.content = Content(config, service.platforms)

    async def session(self, incoming, kind, payload):
        await self.db.execute('INSERT INTO admin_sessions VALUES(?,?,?,?) ON CONFLICT(platform,user_id) DO UPDATE SET kind=excluded.kind,payload=excluded.payload',
                              (incoming.platform, incoming.user_id, kind, dumps(payload)))

    async def handle(self, incoming):
        if not self.config.admin(incoming.platform, incoming.user_id):
            return
        parts = incoming.callback.split(':') if incoming.callback else ['campaign','new']
        action = parts[1]
        if action == 'new':
            await self.service.say(incoming, '✉ Новая рассылка\nВыберите аудиторию.',
                                   [[button('Действующие','campaign:audience:active'),button('Завершённые','campaign:audience:completed')],
                                    [button('Все','campaign:audience:all')],[button('↩ В меню','admin:home')]])
            return
        if action == 'audience':
            audience = parts[2]
            if audience not in {'active','completed','all'}:
                return
            async with self.db.transaction():
                await self.db.execute("UPDATE campaigns SET state='cancelled' WHERE platform=? AND admin_id=? AND state='draft'", (incoming.platform,incoming.user_id))
                cursor = await self.db.execute('INSERT INTO campaigns(platform,admin_id,audience,created_at) VALUES(?,?,?,?)', (incoming.platform,incoming.user_id,audience,stamp()))
                cid = cursor.lastrowid
                await self.session(incoming,'campaign',{'campaign_id':cid})
            await self.service.say(incoming, 'Отправьте сообщение так, как его должен увидеть клиент: текст с форматированием, фото, видео, документ или вложение. Альбом можно отправить целиком.\nДля нескольких сообщений используйте «Добавить часть».', [[button('Отмена',f'campaign:cancel:{cid}')]])
            return
        try:
            cid = int(parts[2])
        except (ValueError, IndexError):
            return
        row = await self.db.one('SELECT * FROM campaigns WHERE id=?', (cid,))
        if not row:
            return
        if action == 'progress':
            await self.progress(incoming,cid)
            return
        if row['state'] != 'draft':
            await self.service.say(incoming,'Эта кампания уже отправлена или отменена.',[[button('Прогресс',f'campaign:progress:{cid}')]])
            return
        if action == 'cancel':
            await self.db.execute("UPDATE campaigns SET state='cancelled' WHERE id=? AND state='draft'", (cid,))
            await self.db.execute("DELETE FROM admin_sessions WHERE kind IN ('campaign','variant') AND json_extract(payload,'$.campaign_id')=?", (cid,))
            await self.service.say(incoming,'Кампания отменена.',[[button('В меню','admin:home')]])
        elif action in {'edit','append'}:
            async with self.db.transaction():
                if action == 'edit':
                    # Draft items have no recipients yet; remove variant cache before replacing content.
                    await self.db.execute('DELETE FROM campaign_variants WHERE item_id IN (SELECT id FROM campaign_items WHERE campaign_id=?)', (cid,))
                    await self.db.execute('DELETE FROM campaign_items WHERE campaign_id=?', (cid,))
                    await self.db.execute('UPDATE campaigns SET revision=revision+1,preview_revision=NULL WHERE id=?', (cid,))
                await self.session(incoming,'campaign',{'campaign_id':cid})
            await self.service.say(incoming,'Отправьте новое сообщение или альбом.',[[button('Отмена',f'campaign:cancel:{cid}')]])
        elif action == 'preview':
            await self.db.execute('UPDATE campaigns SET revision=revision+1,preview_revision=NULL WHERE id=? AND state=\'draft\'',(cid,))
            await self.preview(cid)
        elif action == 'variant':
            target = parts[3]
            if target != incoming.platform:
                await self.service.say(incoming,'Добавьте вариант в соответствующем мессенджере через /admin → Кампании → номер кампании.')
                return
            items = await self.db.all('SELECT id FROM campaign_items WHERE campaign_id=? ORDER BY id', (cid,))
            if len(items) != 1:
                await self.service.say(incoming,'Для отдельных вариантов создайте кампанию из одного сообщения. Для нескольких частей редактируйте исходное содержимое.')
                return
            await self.session(incoming,'variant',{'campaign_id':cid,'item_id':items[0]['id'],'platform':target})
            await self.service.say(incoming,'Отправьте нативный вариант сообщения для этого мессенджера. После него предпросмотр обновится.')
        elif action == 'send':
            await self.launch(incoming, cid, int(parts[3]))

    async def consume(self,incoming):
        session = await self.db.one('SELECT * FROM admin_sessions WHERE platform=? AND user_id=?', (incoming.platform,incoming.user_id))
        if not session or session['kind'] not in {'campaign','variant'} or incoming.text.startswith('/') or incoming.callback:
            return False
        state = json.loads(session['payload'])
        cid = state['campaign_id']
        campaign = await self.db.one("SELECT * FROM campaigns WHERE id=? AND state='draft'", (cid,))
        if not campaign:
            await self.db.execute('DELETE FROM admin_sessions WHERE platform=? AND user_id=?', (incoming.platform,incoming.user_id))
            return False
        try:
            source = capture(incoming.platform,incoming.raw or {})
        except (ValueError,KeyError) as exc:
            await self.service.say(incoming,str(exc))
            return True
        async with self.db.transaction():
            if session['kind'] == 'variant':
                await self.db.execute('INSERT INTO campaign_variants VALUES(?,?,?) ON CONFLICT(item_id,platform) DO UPDATE SET payload=excluded.payload', (state['item_id'],incoming.platform,dumps({'method':'copy','source':source})))
            else:
                key = f"{incoming.platform}:{source.get('message_id',source.get('mid'))}"
                if source.get('group'):
                    key = incoming.platform+':group:'+source['group']
                    existing = await self.db.one('SELECT * FROM campaign_items WHERE campaign_id=? AND source_key=?',(cid,key))
                    if existing:
                        original = json.loads(existing['content'])
                        ids = original.get('message_ids',[original['message_id']])
                        if source['message_id'] not in ids:
                            original['message_ids'] = sorted([*ids,source['message_id']])
                            original['media'] += source['media']
                            original['unsupported_format'] = bool(original.get('unsupported_format') or source.get('unsupported_format'))
                            if source['text']:
                                original['text'] += '\n'+source['text']
                                original['html'] += '\n'+source['html']
                            await self.db.execute('UPDATE campaign_items SET content=? WHERE id=?',(dumps(original),existing['id']))
                            await self.db.execute('DELETE FROM campaign_variants WHERE item_id=?',(existing['id'],))
                    else:
                        source['message_ids'] = [source['message_id']]
                        await self.db.execute('INSERT INTO campaign_items(campaign_id,source_key,content) VALUES(?,?,?)',(cid,key,dumps(source)))
                else:
                    await self.db.execute('INSERT OR IGNORE INTO campaign_items(campaign_id,source_key,content) VALUES(?,?,?)', (cid,key,dumps(source)))
            await self.db.execute('UPDATE campaigns SET revision=revision+1,preview_revision=NULL WHERE id=?', (cid,))
            row = await self.db.one('SELECT revision FROM campaigns WHERE id=?', (cid,))
            await self.db.enqueue(f"preview:{cid}:{row['revision']}",'campaign_preview',{'campaign_id':cid,'revision':row['revision']},at=now()+timedelta(seconds=2))
            # Albums arrive as separate messages; keep collector active for the group only.
            if not source.get('group'):
                await self.db.execute('DELETE FROM admin_sessions WHERE platform=? AND user_id=?', (incoming.platform,incoming.user_id))
        return True

    async def recipients(self, audience):
        rows = await self.db.all('SELECT * FROM clients WHERE opted_out=0 AND merged_into IS NULL ORDER BY id')
        result = []
        for client in rows:
            if audience != 'all' and client['segment'] != audience:
                continue
            sub = await self.scheduler.channel(client)
            if sub:
                result.append((client,sub))
        return result

    async def preview(self,cid,revision=None):
        campaign = await self.db.one("SELECT * FROM campaigns WHERE id=? AND state='draft'", (cid,))
        if not campaign or (revision is not None and revision != campaign['revision']):
            return
        self.service.context,self.service.sequence = f"preview-{cid}-{campaign['revision']}",0
        owner = Incoming(campaign['platform'],campaign['admin_id'],campaign['admin_id'])
        recipients = await self.recipients(campaign['audience'])
        targets = {s['platform'] for _,s in recipients} | {campaign['platform']}
        items = await self.db.all('SELECT * FROM campaign_items WHERE campaign_id=? ORDER BY id', (cid,))
        if not items:
            await self.service.say(owner,'Сначала добавьте сообщение.')
            return
        try:
            for item in items:
                source = json.loads(item['content'])
                for target in sorted(targets):
                    variant = await self.db.one('SELECT payload FROM campaign_variants WHERE item_id=? AND platform=?', (item['id'],target))
                    if not variant:
                        payload = await self.content.prepare(source,target)
                        await self.db.execute('INSERT INTO campaign_variants VALUES(?,?,?)', (item['id'],target,dumps(payload)))
            async with self.db.transaction():
                for target in sorted(targets):
                    uid = self.config.admin_id if target == 'telegram' else self.config.max_admin_id
                    await self.db.enqueue(f"preview-content:{cid}:{campaign['revision']}:{target}:{int(self.config.test_mode)}",'campaign_preview_send',
                                          {'campaign_id':cid,'revision':campaign['revision'],'platform':target,'user_id':uid,'chat_id':uid})
                # Confirmation buttons are sent only after every platform preview was delivered.
                await self.db.enqueue(f"preview-ready:{cid}:{campaign['revision']}:{int(self.config.test_mode)}",'campaign_preview_ready',{'campaign_id':cid,'revision':campaign['revision'],'targets':sorted(targets)})
        except (ValueError,APIError) as exc:
            await self.service.say(owner,'Предпросмотр не готов: '+str(exc)+'\nНичего не будет отправлено до исправления.',
                                   [[button('Изменить',f'campaign:edit:{cid}')],[button('Вариант Telegram',f'campaign:variant:{cid}:telegram'),button('Вариант MAX',f'campaign:variant:{cid}:max')],
                                    [button('Отмена',f'campaign:cancel:{cid}')]])

    async def preview_send(self,payload):
        campaign = await self.db.one('SELECT * FROM campaigns WHERE id=?', (payload['campaign_id'],))
        if campaign['state'] != 'draft' or campaign['revision'] != payload['revision']:
            return
        variants = await self.db.all('SELECT v.item_id,v.payload FROM campaign_variants v JOIN campaign_items i ON i.id=v.item_id WHERE i.campaign_id=? AND v.platform=? ORDER BY i.id', (campaign['id'],payload['platform']))
        for row in variants:
            variant = json.loads(row['payload'])
            external = await self.content.send(variant,payload['platform'],payload['user_id'],payload['chat_id'])
            # Native copies are frozen to the bot's preview message, not the mutable admin original.
            if not self.config.test_mode and variant['method'] == 'copy':
                source = variant['source']
                if payload['platform'] == 'telegram':
                    source['chat_id'] = payload['chat_id']
                    ids = [int(x) for x in external.split(',')]
                    source['message_id'] = ids[0]
                    if len(ids) > 1:
                        source['message_ids'] = ids
                else:
                    source['mid'] = external
                await self.db.execute('UPDATE campaign_variants SET payload=? WHERE item_id=? AND platform=?',(dumps(variant),row['item_id'],payload['platform']))

    async def preview_ready(self,payload):
        cid,revision = payload['campaign_id'],payload['revision']
        campaign = await self.db.one("SELECT * FROM campaigns WHERE id=? AND state='draft' AND revision=?", (cid,revision))
        if not campaign:
            return
        for platform in payload['targets']:
            job = await self.db.one('SELECT state FROM jobs WHERE key=?', (f'preview-content:{cid}:{revision}:{platform}:{int(self.config.test_mode)}',))
            if not job or job['state'] != 'done':
                raise RetryLater('preview_not_delivered',5)
        recipients = await self.recipients(campaign['audience'])
        owner = Incoming(campaign['platform'],campaign['admin_id'],campaign['admin_id'])
        await self.db.execute('UPDATE campaigns SET preview_revision=?,preview_test_mode=?,preview_recipients=? WHERE id=? AND revision=?', (revision,int(self.config.test_mode),dumps([(c['id'],s['id']) for c,s in recipients]),cid,revision))
        await self.service.say(owner,f"Предпросмотр кампании #{cid}\nАудитория: {campaign['audience']}\nПолучателей сейчас: {len(recipients)}\nПредпросмотр отправлен администраторам: {', '.join(payload['targets'])}.\nTEST_MODE: {self.config.test_mode}; рассылки: {self.config.broadcasts_enabled}\nЧисло уточняется при запуске и учитывает отписки.",
                               [[button('Отправить',f'campaign:send:{cid}:{revision}')],
                                [button('Изменить',f'campaign:edit:{cid}'),button('Добавить часть',f'campaign:append:{cid}')],
                                [button('Отмена',f'campaign:cancel:{cid}')]])

    async def launch(self,incoming,cid,revision):
        if self.config.test_mode or not self.config.broadcasts_enabled:
            await self.service.say(incoming,'Отправка отключена: TEST_MODE должен быть false, BROADCASTS_ENABLED — true. Черновик сохранён.')
            return
        async with self.db.transaction():
            campaign = await self.db.one("SELECT * FROM campaigns WHERE id=? AND state='draft' AND revision=? AND preview_revision=? AND preview_test_mode=0", (cid,revision,revision))
            if not campaign:
                await self.service.say(incoming,'Кампания уже запущена или нужен новый предпросмотр в production.',[[button('Предпросмотр',f'campaign:preview:{cid}')]])
                return
            recipients = await self.recipients(campaign['audience'])
            if dumps([(c['id'],s['id']) for c,s in recipients]) != campaign['preview_recipients']:
                await self.service.say(incoming,'Изменилась аудитория или предпочитаемые каналы. Обновите предпросмотр перед отправкой.',[[button('Предпросмотр',f'campaign:preview:{cid}')]])
                return
            targets = {s['platform'] for _,s in recipients}
            items = await self.db.all('SELECT id FROM campaign_items WHERE campaign_id=?', (cid,))
            if not items:
                return
            for item in items:
                for target in targets:
                    if not await self.db.one('SELECT 1 FROM campaign_variants WHERE item_id=? AND platform=?', (item['id'],target)):
                        await self.service.say(incoming,'Изменилась аудитория: нужен новый предпросмотр.',[[button('Предпросмотр',f'campaign:preview:{cid}')]])
                        return
            await self.db.execute("UPDATE campaigns SET state='running',started_at=? WHERE id=?", (stamp(),cid))
            for client,sub in recipients:
                cursor = await self.db.execute('INSERT INTO campaign_deliveries(campaign_id,client_id,subscription_id,created_at) VALUES(?,?,?,?)', (cid,client['id'],sub['id'],stamp()))
                did = cursor.lastrowid
                for item in items:
                    await self.db.execute('INSERT INTO campaign_parts(delivery_id,item_id) VALUES(?,?)', (did,item['id']))
                await self.db.enqueue(f'campaign-delivery:{did}','campaign_delivery',{'delivery_id':did})
            await self.db.event('campaign_started',detail={'campaign_id':cid,'recipients':len(recipients),'actor':f'{incoming.platform}:{incoming.user_id}'})
            await self.db.execute("DELETE FROM admin_sessions WHERE kind IN ('campaign','variant') AND json_extract(payload,'$.campaign_id')=?", (cid,))
            if not recipients:
                await self.db.execute("UPDATE campaigns SET state='completed',completed_at=? WHERE id=?", (stamp(),cid))
        await self.service.say(incoming,f'Кампания #{cid} поставлена в очередь: {len(recipients)} получателей.',[[button('Прогресс',f'campaign:progress:{cid}')]])

    async def finish(self,cid):
        if not await self.db.one("SELECT 1 FROM campaign_deliveries WHERE campaign_id=? AND state IN ('pending','retry','sending')", (cid,)):
            await self.db.execute("UPDATE campaigns SET state='completed',completed_at=? WHERE id=? AND state='running'", (stamp(),cid))

    async def deliver(self,did):
        row = await self.db.one('SELECT * FROM campaign_deliveries WHERE id=?', (did,))
        if not row or row['state'] not in {'pending','retry'}:
            return
        if self.config.test_mode or not self.config.broadcasts_enabled:
            raise RetryLater('broadcasts_disabled',3600)
        campaign = await self.db.one('SELECT * FROM campaigns WHERE id=?', (row['campaign_id'],))
        client = await self.db.canonical(row['client_id'])
        await self.service.reconcile_client(client['id'])
        client = await self.db.canonical(client['id'])
        family = await self.db.family(client['id'])
        marks = ','.join('?' for _ in family)
        duplicate = await self.db.one(f"SELECT 1 FROM campaign_deliveries WHERE campaign_id=? AND client_id IN ({marks}) AND id<>? AND (state IN ('sent','sending','unknown') OR (id<? AND state IN ('pending','retry')))", (row['campaign_id'],*family,did,did))
        sub = await self.scheduler.channel(client)
        if client['opted_out'] or not sub or duplicate or (campaign['audience'] != 'all' and campaign['audience'] != client['segment']):
            state = 'duplicate' if duplicate else 'unsubscribed' if client['opted_out'] else 'skipped'
            await self.db.execute('UPDATE campaign_deliveries SET state=? WHERE id=?',(state,did))
            await self.finish(row['campaign_id'])
            return
        await self.db.execute("UPDATE campaign_deliveries SET state='sending',subscription_id=? WHERE id=?",(sub['id'],did))
        try:
            for part in await self.db.all('SELECT * FROM campaign_parts WHERE delivery_id=? ORDER BY item_id',(did,)):
                if part['state'] == 'sent':
                    continue
                if part['state'] == 'unknown':
                    raise AmbiguousSend('campaign',0,'previous_part_unknown')
                variant = await self.db.one('SELECT payload FROM campaign_variants WHERE item_id=? AND platform=?',(part['item_id'],sub['platform']))
                if not variant:
                    raise ValueError('Предпросмотр для нового канала отсутствует')
                # Poll ingestion applies /stop concurrently: reread before every part.
                if (await self.db.canonical(client['id']))['opted_out']:
                    await self.db.execute("UPDATE campaign_deliveries SET state='unsubscribed' WHERE id=?",(did,))
                    await self.finish(row['campaign_id'])
                    return
                await self.db.execute("UPDATE campaign_parts SET state='sending' WHERE delivery_id=? AND item_id=?",(did,part['item_id']))
                external = await self.content.send(json.loads(variant['payload']),sub['platform'],sub['user_id'],sub['chat_id'])
                await self.db.execute("UPDATE campaign_parts SET state='sent',external_id=? WHERE delivery_id=? AND item_id=?",(external,did,part['item_id']))
        except AmbiguousSend as exc:
            await self.db.execute("UPDATE campaign_parts SET state='unknown',error=? WHERE delivery_id=? AND state='sending'",(str(exc),did))
            await self.db.execute("UPDATE campaign_deliveries SET state='unknown',error=? WHERE id=?",(str(exc),did))
            await self.finish(row['campaign_id'])
            raise
        except APIError as exc:
            state = 'blocked' if exc.status == 403 else 'retry'
            await self.db.execute("UPDATE campaign_parts SET state='pending',error=? WHERE delivery_id=? AND state='sending'",(str(exc),did))
            await self.db.execute('UPDATE campaign_deliveries SET state=?,error=? WHERE id=?',(state,str(exc),did))
            if state == 'blocked':
                await self.db.execute('UPDATE subscriptions SET blocked=1 WHERE id=?',(sub['id'],))
                # Switching after some parts were sent could split/duplicate a campaign: require next campaign.
                await self.finish(row['campaign_id'])
                return
            raise
        except Exception as exc:
            in_flight = await self.db.one("SELECT 1 FROM campaign_parts WHERE delivery_id=? AND state='sending'",(did,))
            if in_flight:
                await self.db.execute("UPDATE campaign_parts SET state='unknown',error='unexpected_response' WHERE delivery_id=? AND state='sending'",(did,))
                await self.db.execute("UPDATE campaign_deliveries SET state='unknown',error='unexpected_response' WHERE id=?",(did,))
                await self.finish(row['campaign_id'])
                raise AmbiguousSend('campaign',0,type(exc).__name__) from None
            raise
        await self.db.execute("UPDATE campaign_deliveries SET state='sent',sent_at=?,error=NULL WHERE id=?",(stamp(),did))
        await self.db.event('campaign_success',sub['id'],client['id'],{'campaign_id':row['campaign_id'],'delivery_id':did})
        await self.finish(row['campaign_id'])

    async def progress(self,incoming,cid):
        campaign = await self.db.one('SELECT * FROM campaigns WHERE id=?',(cid,))
        rows = await self.db.all('SELECT state,COUNT(*) n FROM campaign_deliveries WHERE campaign_id=? GROUP BY state',(cid,))
        counts = {x['state']:x['n'] for x in rows}
        text = f"Кампания #{cid} · {campaign['state']} · {campaign['audience']}\nВсего: {sum(counts.values())}\nУспешно: {counts.get('sent',0)}\nОжидают: {counts.get('pending',0)+counts.get('retry',0)+counts.get('sending',0)}\nОшибки: {counts.get('failed',0)+counts.get('blocked',0)}\nНеизвестный результат: {counts.get('unknown',0)}\nОтписались: {counts.get('unsubscribed',0)}\nПропущены / объединены: {counts.get('skipped',0)} / {counts.get('duplicate',0)}"
        buttons = [[button('Обновить',f'campaign:progress:{cid}')]]
        if campaign['state'] == 'draft':
            buttons += [[button('Предпросмотр',f'campaign:preview:{cid}')],[button('Вариант Telegram',f'campaign:variant:{cid}:telegram'),button('Вариант MAX',f'campaign:variant:{cid}:max')]]
        buttons += [[button('История кампаний','admin:campaigns:0')]]
        await self.service.say(incoming,text,buttons)
