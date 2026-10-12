import json
from datetime import datetime,timedelta
from .db import now, stamp, dumps
from .platforms.base import button,link_button
from .scheduler import TZ

PAGE_SIZE = 5


def navigation(section,page,more):
    row = []
    if page:
        row.append(button('←',f'admin:{section}:{page-1}'))
    if more:
        row.append(button('→',f'admin:{section}:{page+1}'))
    return ([row] if row else [])+[[button('↩ В меню','admin:home')]]


class Admin:
    def __init__(self,config,db,service,scheduler):
        self.config,self.db,self.service,self.scheduler = config,db,service,scheduler

    def local_date(self,value):
        return datetime.fromisoformat(value).astimezone(TZ).strftime('%d.%m.%Y %H:%M') if value else '—'

    async def handle(self,incoming):
        if not self.config.admin(incoming.platform,incoming.user_id):
            return
        parts = (incoming.callback or 'admin:home').split(':')
        try:
            section = parts[1]
            if section == 'home':
                n = (await self.db.one("SELECT COUNT(*) n FROM subscriptions WHERE client_id IS NOT NULL AND claimed_lead_id IS NULL"))['n']
                await self.service.say(incoming,'◆ СИНАЙ · Центр управления',[
                    [button('📊 Статистика','admin:stats:all'),button('👥 Подписчики','admin:subscribers:0')],
                    [button(f'Несинхронизированные · {n}','admin:unsynced:0')],
                    [button('✉ Создать рассылку','campaign:new'),button('Кампании','admin:campaigns:0')],
                    [button('Ежемесячные · история','admin:deliveries:0'),button('Ближайшие','admin:upcoming:0')],
                    [button('⚠ Ошибки / polling','admin:errors:0'),button('Интервалы','admin:settings')],
                    [button('🔄 Повторная сверка','admin:reconcile')]])
            elif section == 'stats':
                await self.stats(incoming,parts[2])
            elif section in {'subscribers','unsynced','campaigns','deliveries','errors','upcoming'}:
                await self.list_page(incoming,section,max(0,int(parts[2])))
            elif section == 'subscriber':
                await self.subscriber_page(incoming,int(parts[2]))
            elif section == 'attach':
                sid = int(parts[2])
                await self.db.execute('INSERT INTO admin_sessions VALUES(?,?,?,?) ON CONFLICT(platform,user_id) DO UPDATE SET kind=excluded.kind,payload=excluded.payload',
                                      (incoming.platform,incoming.user_id,'attach',dumps({'subscription_id':sid})))
                await self.service.say(incoming,'Введите lead_id существующей сделки. Привязка не подтверждает личность клиента.',[[button('Отмена','admin:cancel')]])
            elif section == 'cancel':
                await self.db.execute('DELETE FROM admin_sessions WHERE platform=? AND user_id=?',(incoming.platform,incoming.user_id))
                await self.service.say(incoming,'Ввод отменён.',[[button('В меню','admin:home')]])
            elif section == 'retry':
                sid = int(parts[2])
                await self.db.enqueue(f'admin-sync:{self.service.context}:{sid}','sync',{'subscription_id':sid})
                await self.service.say(incoming,'Повторный поиск только по телефону поставлен в очередь.',[[button('Назад',f'admin:subscriber:{sid}')]])
            elif section == 'retryjob':
                jid = int(parts[2])
                job = await self.db.one('SELECT * FROM jobs WHERE id=?',(jid,))
                if job and job['kind'] in {'sync','finalize','reconcile_client','note','pipeline'}:
                    await self.db.execute("UPDATE jobs SET state='pending',attempts=0,available_at=? WHERE id=?",(stamp(),jid))
                await self.service.say(incoming,'Повтор поставлен в очередь.',[[button('Ошибки','admin:errors:0')]])
            elif section == 'settings':
                await self.service.say(incoming,f"Интервалы: active — {await self.scheduler.interval('active')} мес.; completed — {await self.scheduler.interval('completed')} мес.\nTEST_MODE={self.config.test_mode}\nBROADCASTS_ENABLED={self.config.broadcasts_enabled}\nPOLLING_ENABLED={self.config.polling_enabled}",
                    [[button('active · 1','admin:interval:active:1'),button('active · 2','admin:interval:active:2')],
                     [button('completed · 1','admin:interval:completed:1'),button('completed · 2','admin:interval:completed:2')],
                     [button('В меню','admin:home')]])
            elif section == 'interval':
                segment,interval = parts[2],int(parts[3])
                if segment not in {'active','completed'} or interval not in {1,2}:
                    return
                await self.db.set_setting(segment+'_interval_months',interval)
                await self.db.event('interval_changed',detail={'segment':segment,'months':interval,'actor':f'{incoming.platform}:{incoming.user_id}'})
                await self.service.say(incoming,'Сохранено. Даты подписки сохранены.',[[button('Настройки','admin:settings')]])
            elif section == 'reconcile':
                for row in await self.db.all('SELECT id FROM clients WHERE merged_into IS NULL'):
                    await self.db.enqueue(f"admin-reconcile:{self.service.context}:{row['id']}",'reconcile_client',{'client_id':row['id']})
                for sub in await self.db.all('SELECT id FROM subscriptions WHERE phone IS NOT NULL AND identity_verified=0'):
                    await self.db.enqueue(f"admin-resync:{self.service.context}:{sub['id']}",'sync',{'subscription_id':sub['id']})
                await self.service.say(incoming,'Сверка и повторный поиск по телефонам поставлены в очередь.',[[button('В меню','admin:home')]])
        except (IndexError,ValueError,KeyError):
            await self.service.say(incoming,'Запись недоступна. Обновите меню.',[[button('В меню','admin:home')]])

    async def consume(self,incoming):
        row = await self.db.one("SELECT * FROM admin_sessions WHERE platform=? AND user_id=? AND kind='attach'",(incoming.platform,incoming.user_id))
        if not row or incoming.callback or incoming.text.startswith('/'):
            return False
        sid = json.loads(row['payload'])['subscription_id']
        try:
            if not incoming.text.isascii() or not incoming.text.isdigit() or not 0 < int(incoming.text) < 2**63:
                raise ValueError('Введите положительный числовой lead_id.')
            sub = await self.db.one('SELECT * FROM subscriptions WHERE id=?',(sid,))
            if not sub or not sub['client_id']:
                raise ValueError('Подписчик не найден')
            lead = await self.service.crm.lead(int(incoming.text))
            ok = await self.service.attach(sub,lead,'admin',actor=f'{incoming.platform}:{incoming.user_id}')
            if not ok:
                raise ValueError('Сделка занята незавершённой регистрацией или аккаунт уже связан с другой сделкой.')
            await self.db.execute('DELETE FROM admin_sessions WHERE platform=? AND user_id=?',(incoming.platform,incoming.user_id))
            await self.service.say(incoming,'Привязка сохранена без повторного примечания. Рассылки учитывают объединённого клиента.',[[button('Карточка',f'admin:subscriber:{sid}')]])
        except ValueError as exc:
            await self.service.say(incoming,str(exc),[[button('Отмена','admin:cancel')]])
        return True

    async def subscriber_page(self,incoming,sid):
        sub = await self.db.one('SELECT * FROM subscriptions WHERE id=?',(sid,))
        client = await self.db.canonical(sub['client_id']) if sub['client_id'] else None
        text = f"Аккаунт #{sid}\n{sub['platform']} · user_id: {sub['user_id']}\nПодписка: {self.local_date(sub['subscribed_at'])}\nВыбрано: {sub['selected_segment'] or '—'}\nТекущий сегмент: {client['segment'] if client else '—'}\nСделка: {sub['claimed_lead_id'] or 'нет'}\nМетод: {sub['crm_method'] or 'нет'}\nЛичность: {'подтверждена телефоном' if sub['identity_verified'] else 'не подтверждена'}\nОтписан: {bool(sub['stopped'])}"
        buttons = []
        if sub['claimed_lead_id']:
            buttons.append([link_button('Сделка amoCRM',f"{self.config.amocrm_base_url}/leads/detail/{sub['claimed_lead_id']}")])
        else:
            buttons.append([button('Привязать lead_id',f'admin:attach:{sid}')])
        if client and client['contact_id']:
            buttons.append([link_button('Контакт amoCRM',f"{self.config.amocrm_base_url}/contacts/detail/{client['contact_id']}")])
        buttons += [[button('Проверить телефон',f'admin:retry:{sid}')],[button('↩ Несинхронизированные','admin:unsynced:0')],[button('В меню','admin:home')]]
        await self.service.say(incoming,text,buttons)

    async def stats(self,incoming,period):
        local = now().astimezone(TZ)
        since = {'today':stamp(local.replace(hour=0,minute=0,second=0,microsecond=0)), '7':stamp(now()-timedelta(days=7)), '30':stamp(now()-timedelta(days=30)), 'all':''}.get(period,'')
        clients = await self.db.all('SELECT segment,identity_verified,COUNT(*) n FROM clients WHERE merged_into IS NULL AND created_at>=? GROUP BY segment,identity_verified',(since,))
        subs = {r['platform']:r['n'] for r in await self.db.all('SELECT platform,COUNT(*) n FROM subscriptions WHERE subscribed_at>=? GROUP BY platform',(since,))}
        events = {r['kind']:r['n'] for r in await self.db.all('SELECT kind,COUNT(*) n FROM events WHERE created_at>=? GROUP BY kind',(since,))}
        unsynced = (await self.db.one('SELECT COUNT(*) n FROM subscriptions WHERE subscribed_at>=? AND claimed_lead_id IS NULL',(since,)))['n']
        stopped = (await self.db.one('SELECT COUNT(*) n FROM clients WHERE merged_into IS NULL AND opted_out=1 AND created_at>=?',(since,)))['n']
        plan = await self.scheduler.plan()
        text = f"📊 {period}\nУникальные подтверждённые CRM-клиенты: {sum(r['n'] for r in clients if r['identity_verified'])}\nПолучатели (локальные/объединённые): {sum(r['n'] for r in clients)}\nАккаунты Telegram / MAX: {subs.get('telegram',0)} / {subs.get('max',0)}\nactive / completed: {sum(r['n'] for r in clients if r['segment']=='active')} / {sum(r['n'] for r in clients if r['segment']=='completed')}\nНесинхронизированные аккаунты: {unsynced}\nОтписавшиеся получатели: {stopped}\nАвтосопоставления по телефону: {events.get('matched_auto',0)}\nНеподтверждённые привязки: {events.get('matched_unverified',0)}\nУспешные отправки: {events.get('send_success',0)+events.get('campaign_success',0)}\nОшибки ежемесячных: {events.get('send_error',0)}\nБлижайшая: {self.local_date(plan[0]['due_at']) if plan else 'нет'}\n\nГруппы и получатели — по дате создания; аккаунты — по подписке; операции — события периода. Локальные аккаунты и числовые ссылки не доказывают уникальность человека. Подробности ошибок и прогресс произвольных рассылок — в кампаниях."
        await self.service.say(incoming,text,[[button('Сегодня','admin:stats:today'),button('7 дней','admin:stats:7')],[button('30 дней','admin:stats:30'),button('Всё время','admin:stats:all')],[button('В меню','admin:home')]])

    async def list_page(self,incoming,section,page):
        offset = page*PAGE_SIZE
        if section == 'upcoming':
            rows = (await self.scheduler.plan())[offset:offset+PAGE_SIZE+1]
        else:
            table = {'subscribers':'subscriptions','unsynced':'subscriptions','campaigns':'campaigns','deliveries':'deliveries','errors':'errors'}[section]
            where = 'WHERE client_id IS NOT NULL AND claimed_lead_id IS NULL' if section == 'unsynced' else ''
            rows = await self.db.all(f'SELECT * FROM {table} {where} ORDER BY id DESC LIMIT ? OFFSET ?',(PAGE_SIZE+1,offset))
        lines,buttons = [f'{section} · страница {page+1}'],[]
        for row in rows[:PAGE_SIZE]:
            if section in {'subscribers','unsynced'}:
                lines.append(f"#{row['id']} · {row['platform']} · {row['user_id']}\n{self.local_date(row['subscribed_at'])} · {row['selected_segment'] or '—'}"+(" · отписан" if row['stopped'] else ''))
                buttons.append([button(f"Аккаунт #{row['id']}",f"admin:subscriber:{row['id']}")])
            elif section == 'campaigns':
                lines.append(f"#{row['id']} · {row['audience']} · {row['state']} · {self.local_date(row['created_at'])}")
                buttons.append([button(f"Кампания #{row['id']}",f"campaign:progress:{row['id']}")])
            elif section == 'deliveries':
                lines.append(f"#{row['id']} · клиент {row['client_id']} · {row['segment']} · {row['state']} · {self.local_date(row['due_at'])}\n{row['error'] or ''}")
            elif section == 'errors':
                lines.append(f"#{row['id']} · {row['kind']} · {row['message'][:150]}"+(' · исправлено' if row['resolved_at'] else ''))
                if row['job_id']:
                    buttons.append([button(f"Перепроверить #{row['id']}",f"admin:retryjob:{row['job_id']}")])
            else:
                lines.append(f"Клиент #{row['client_id']} · {row['platform']} · {row['segment']} · {self.local_date(row['due_at'])}")
        if not rows:
            lines.append('Записей пока нет.')
        await self.service.say(incoming,'\n\n'.join(lines),buttons+navigation(section,page,len(rows)>PAGE_SIZE))
