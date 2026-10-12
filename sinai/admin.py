import json
from datetime import timedelta
from .db import now, stamp
from .platforms.base import button, link_button
from .scheduler import TZ

PAGE_SIZE = 5


def navigation(section, page, has_next):
    row = []
    if page:
        row.append(button("←", f"admin:{section}:{page-1}"))
    if has_next:
        row.append(button("→", f"admin:{section}:{page+1}"))
    return ([row] if row else []) + [[button("↩ В меню", "admin:home")]]


class Admin:
    def __init__(self, config, db, service, scheduler):
        self.config, self.db, self.service, self.scheduler = config, db, service, scheduler

    async def handle(self, incoming):
        if not self.config.admin(incoming.platform, incoming.user_id):
            return
        parts = (incoming.callback or "admin:home").split(":")
        section = parts[1]
        actor = f"{incoming.platform}:{incoming.user_id}"
        try:
            if section == "home":
                await self.service.say(incoming, "◆ СИНАЙ · Центр управления\n\nКлиенты, CRM и рассылки в одном месте.", [
                    [button("📊 Статистика", "admin:stats:all"), button("👥 Подписчики", "admin:subscribers:0")],
                    [button("🔎 На проверке", "admin:reviews:0"), button("✉ История рассылок", "admin:deliveries:0")],
                    [button("⚠ Ошибки CRM", "admin:errors:0"), button("🗓 Ближайшие", "admin:upcoming:0")],
                    [button("⚙ Интервалы", "admin:settings"), button("🔄 Сверить всех", "admin:reconcile")]])
            elif section == "stats":
                await self.stats(incoming, parts[2] if len(parts) > 2 else "all")
            elif section in {"subscribers", "reviews", "deliveries", "errors", "upcoming"}:
                await self.list_page(incoming, section, max(0, int(parts[2])))
            elif section == "review":
                await self.review_page(incoming, int(parts[2]), max(0, int(parts[3])))
            elif section == "choose":
                review_id, version, index = int(parts[2]), int(parts[3]), int(parts[4])
                review = await self.db.one("SELECT * FROM reviews WHERE id=? AND status='pending'", (review_id,))
                if not review or review["version"] != version:
                    raise ValueError("Заявка уже обработана.")
                candidate = json.loads(review["candidates"])[index]
                await self.service.say(incoming, f"Подтвердить привязку?\n{candidate['name']}\nКонтакт {candidate['contact_id']} → сделка {candidate['lead_id']}\nПодтверждение означает, что вы проверили личность клиента, включая возможное расхождение телефона.",
                    [[button("✓ Подтвердить", f"admin:confirm:{review_id}:{version}:{index}")],
                     [button("↩ К заявке", f"admin:review:{review_id}:0")]])
            elif section == "confirm":
                review_id, version, index = int(parts[2]), int(parts[3]), int(parts[4])
                review = await self.db.one("SELECT * FROM reviews WHERE id=? AND status='pending'", (review_id,))
                if not review or review["version"] != version:
                    raise ValueError("Заявка уже обработана.")
                candidate = json.loads(review["candidates"])[index]
                sub = await self.db.one("SELECT * FROM subscriptions WHERE id=?", (review["subscription_id"],))
                if sub["stopped"]:
                    raise ValueError("Клиент отписался. Требуется повторная регистрация.")
                await self.service.bind(sub, candidate, "manual", actor, review_id)
                await self.service.say(incoming, "✓ Привязка подтверждена.", [[button("К заявкам", "admin:reviews:0")]])
            elif section == "reject":
                rid = int(parts[2])
                review = await self.db.one("SELECT * FROM reviews WHERE id=? AND status='pending'", (rid,))
                if review:
                    async with self.db.transaction():
                        await self.db.execute("UPDATE reviews SET status='rejected',resolved_at=?,resolved_by=? WHERE id=?", (stamp(), actor, rid))
                        await self.db.execute("UPDATE subscriptions SET state='not_found' WHERE id=?", (review["subscription_id"],))
                        await self.db.event("review_rejected", review["subscription_id"], detail={"actor": actor})
                    sub = await self.db.one("SELECT * FROM subscriptions WHERE id=?", (review["subscription_id"],))
                    await self.service.tell_sub(sub, "Не удалось подтвердить ваши данные. Обратитесь к менеджеру компании «Синай» за персональной ссылкой.")
                await self.service.say(incoming, "Заявка отклонена.", [[button("К заявкам", "admin:reviews:0")]])
            elif section == "retry":
                sub_id = int(parts[2])
                sub = await self.db.one("SELECT * FROM subscriptions WHERE id=?", (sub_id,))
                if sub:
                    await self.db.enqueue(f"manual-sync:{self.service.context}:{sub_id}", "reconcile_client" if sub["client_id"] else "sync",
                                          {"client_id": sub["client_id"]} if sub["client_id"] else {"subscription_id": sub_id, "by_name": bool(sub["name"])})
                await self.service.say(incoming, "Проверка поставлена в очередь.", [[button("В меню", "admin:home")]])
            elif section == "retryjob":
                job_id = int(parts[2])
                job = await self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
                if job and job["kind"] in {"sync", "reconcile_client", "reconcile_lead", "note", "pipeline"}:
                    await self.db.execute("UPDATE jobs SET state='pending',attempts=0,available_at=? WHERE id=?", (stamp(), job_id))
                    await self.db.event("job_retried", detail={"job_id": job_id, "actor": actor})
                await self.service.say(incoming, "Повторная проверка поставлена в очередь.", [[button("К ошибкам", "admin:errors:0")]])
            elif section == "settings":
                active, completed = await self.scheduler.interval("active"), await self.scheduler.interval("completed")
                await self.service.say(incoming, f"⚙ Интервалы рассылок\nДействующие: {active} мес.\nЗавершённые: {completed} мес.\nТестовый режим: {'да' if self.config.test_mode else 'нет'}\nМассовые рассылки: {'включены' if self.config.broadcasts_enabled else 'выключены'}\nНачальная дата подписки сохраняется.",
                    [[button("Действующие: 1 мес.", "admin:interval:active:1"), button("2 мес.", "admin:interval:active:2")],
                     [button("Завершённые: 1 мес.", "admin:interval:completed:1"), button("2 мес.", "admin:interval:completed:2")],
                     [button("↩ В меню", "admin:home")]])
            elif section == "interval":
                segment, interval = parts[2], int(parts[3])
                if segment not in {"active", "completed"} or interval not in {1, 2}:
                    raise ValueError("Недопустимый интервал.")
                await self.db.set_setting(segment + "_interval_months", interval)
                await self.db.event("interval_changed", detail={"segment": segment, "months": interval, "actor": actor})
                await self.service.say(incoming, "Интервал сохранён.", [[button("К настройкам", "admin:settings")]])
            elif section == "reconcile":
                for client in await self.db.all("SELECT id FROM clients"):
                    await self.db.enqueue(f"admin-reconcile:{self.service.context}:{client['id']}", "reconcile_client", {"client_id": client["id"]})
                await self.service.say(incoming, "Сверка всех зарегистрированных клиентов поставлена в очередь.", [[button("В меню", "admin:home")]])
            elif section == "client":
                await self.client_page(incoming, int(parts[2]))
            elif section == "activeask":
                await self.service.say(incoming, "Вернуть клиента в active вручную? Это решение будет записано в историю. Если у контакта есть сделка в завершённой воронке, CRM снова назначит completed.",
                    [[button("Подтвердить active", f"admin:activeconfirm:{int(parts[2])}")], [button("Назад", f"admin:client:{int(parts[2])}")]])
            elif section == "activeconfirm":
                cid = int(parts[2])
                await self.db.execute("UPDATE clients SET segment='active',segment_source='manual' WHERE id=?", (cid,))
                await self.db.event("segment_active_manual", client_id=cid, detail={"actor": actor})
                await self.client_page(incoming, cid)
            elif section == "delivery":
                delivery = await self.db.one("SELECT * FROM deliveries WHERE id=?", (int(parts[2]),))
                if not delivery:
                    raise ValueError("Отправка не найдена.")
                await self.service.say(incoming, f"Отправка №{delivery['id']} · {delivery['state']}\n{delivery['error'] or ''}\n{delivery['text']}\n\nunknown: результат неизвестен, автоматический повтор запрещён. Отметьте как доставленное только после проверки в мессенджере.",
                    ([[button("Доставка проверена", f"admin:delivered:{delivery['id']}")]] if delivery["state"] == "unknown" else []) +
                    [[button("Назад", "admin:deliveries:0")]])
            elif section == "delivered":
                did = int(parts[2])
                await self.db.execute("UPDATE deliveries SET state='sent',sent_at=? WHERE id=? AND state='unknown'", (stamp(), did))
                await self.db.event("delivery_manually_verified", detail={"delivery_id": did, "actor": actor})
                await self.service.say(incoming, "Результат зафиксирован.", [[button("К истории", "admin:deliveries:0")]])
        except (ValueError, IndexError, KeyError):
            await self.service.say(incoming, "Действие недоступно: проверьте заявку или обновите меню.", [[button("В меню", "admin:home")]])

    async def stats(self, incoming, period):
        local = now().astimezone(TZ)
        since = {"today": stamp(local.replace(hour=0, minute=0, second=0, microsecond=0)),
                 "7": stamp(now() - timedelta(days=7)), "30": stamp(now() - timedelta(days=30)), "all": ""}.get(period, "")
        async def count(sql, args=()):
            return (await self.db.one(sql, args))["n"]
        clients = await count("SELECT COUNT(*) n FROM clients WHERE created_at>=?", (since,))
        tg = await count("SELECT COUNT(*) n FROM subscriptions WHERE platform='telegram' AND subscribed_at>=?", (since,))
        mx = await count("SELECT COUNT(*) n FROM subscriptions WHERE platform='max' AND subscribed_at>=?", (since,))
        groups = {x["segment"]: x["n"] for x in await self.db.all("SELECT segment,COUNT(*) n FROM clients WHERE created_at>=? GROUP BY segment", (since,))}
        events = {x["kind"]: x["n"] for x in await self.db.all("SELECT kind,COUNT(*) n FROM events WHERE created_at>=? GROUP BY kind", (since,))}
        auto = await count("SELECT COUNT(DISTINCT client_id) n FROM events WHERE kind='matched_auto' AND created_at>=?", (since,))
        manual = await count("SELECT COUNT(DISTINCT client_id) n FROM events WHERE kind='matched_manual' AND created_at>=?", (since,))
        pending = await count("SELECT COUNT(*) n FROM reviews WHERE status='pending' AND created_at>=?", (since,))
        missing = await count("SELECT COUNT(*) n FROM subscriptions WHERE state='not_found' AND subscribed_at>=?", (since,))
        out = await count("SELECT COUNT(*) n FROM clients WHERE opted_out=1 AND created_at>=?", (since,))
        plan = await self.scheduler.plan()
        text = (f"📊 Статистика · {period}\nУникальные подтверждённые клиенты: {clients}\nПодписки Telegram / MAX: {tg} / {mx}\n"
                f"active / completed: {groups.get('active',0)} / {groups.get('completed',0)}\nАвто / вручную (уникальные): {auto} / {manual}\n"
                f"Заявки в ожидании: {pending}\nАккаунты не найдены в CRM: {missing}\nОтписавшиеся клиенты: {out}\n"
                f"Отписки (события): {events.get('unsubscribe',0)}\nОтправки успешно / ошибки / неизвестно: {events.get('send_success',0)} / {events.get('send_error',0)} / {events.get('send_unknown',0)}\n"
                f"Ближайшая рассылка: {self.local_date(plan[0]['due_at']) if plan else 'нет'}\n\nКлиенты и сегменты — по дате создания клиента; подписки — по дате подписки. Сопоставления и отправки — события периода. Неподтверждённые аккаунты не считаются уникальными клиентами.")
        await self.service.say(incoming, text, [[button("Сегодня", "admin:stats:today"), button("7 дней", "admin:stats:7")],
                                             [button("30 дней", "admin:stats:30"), button("Всё", "admin:stats:all")],
                                             [button("↩ В меню", "admin:home")]])

    def local_date(self, value):
        from datetime import datetime
        return datetime.fromisoformat(value).astimezone(TZ).strftime("%d.%m.%Y %H:%M")

    async def list_page(self, incoming, section, page):
        offset = page * PAGE_SIZE
        tables = {"subscribers": "subscriptions", "reviews": "reviews", "deliveries": "deliveries", "errors": "errors"}
        if section == "upcoming":
            all_rows = await self.scheduler.plan()
            rows = all_rows[offset:offset + PAGE_SIZE + 1]
        else:
            table = tables[section]
            where = "WHERE status='pending'" if section == "reviews" else "WHERE kind IN ('sync','reconcile_client','reconcile_lead','note','pipeline')" if section == "errors" else ""
            rows = await self.db.all(f"SELECT * FROM {table} {where} ORDER BY id DESC LIMIT ? OFFSET ?", (PAGE_SIZE+1, offset))
        titles = {"subscribers": "👥 Подписчики", "reviews": "🔎 Заявки", "deliveries": "✉ История рассылок", "errors": "⚠ Ошибки синхронизации", "upcoming": "🗓 Ближайшие рассылки"}
        lines, buttons = [f"{titles[section]} · страница {page+1}"], []
        for row in rows[:PAGE_SIZE]:
            if section == "subscribers":
                lines.append(f"#{row['id']} {row['name'] or 'Без ФИО'} · {row['platform']} · {row['state']}" + (" · отписан" if row["stopped"] else "") + (" · заблокирован" if row["blocked"] else ""))
                if row["client_id"]:
                    buttons.append([button(f"Карточка #{row['id']}", f"admin:client:{row['client_id']}")])
                buttons.append([button(f"Проверить #{row['id']}", f"admin:retry:{row['id']}")])
            elif section == "reviews":
                lines.append(f"#{row['id']} · {row['reason']}")
                buttons.append([button(f"Заявка #{row['id']}", f"admin:review:{row['id']}:0")])
            elif section == "deliveries":
                lines.append(f"#{row['id']} · клиент {row['client_id']} · {row['segment']} · {row['state']} · {self.local_date(row['due_at'])}")
                buttons.append([button(f"Отправка #{row['id']}", f"admin:delivery:{row['id']}")])
            elif section == "errors":
                lines.append(f"#{row['id']} · {row['kind']} · {row['message'][:180]}" + (" · исправлено" if row["resolved_at"] else ""))
                if row["job_id"] and not row["resolved_at"]:
                    buttons.append([button(f"Повторить #{row['id']}", f"admin:retryjob:{row['job_id']}")])
            else:
                lines.append(f"{row['name']} · {row['platform']} · {row['segment']} · {self.local_date(row['due_at'])}")
        if not rows:
            lines.append("Пока записей нет.")
        await self.service.say(incoming, "\n\n".join(lines), buttons + navigation(section, page, len(rows) > PAGE_SIZE))

    async def review_page(self, incoming, rid, page):
        review = await self.db.one("SELECT * FROM reviews WHERE id=?", (rid,))
        if not review:
            raise ValueError("Заявка не найдена")
        sub = await self.db.one("SELECT * FROM subscriptions WHERE id=?", (review["subscription_id"],))
        candidates = json.loads(review["candidates"])
        start = page * PAGE_SIZE
        lines = [f"🔎 Заявка #{rid} · {review['status']}\n{sub['name'] or 'ФИО не указано'}\n{sub['platform']}:{sub['user_id']} · {sub['phone']}\n{review['reason']}"]
        buttons = []
        for index, row in enumerate(candidates[start:start+PAGE_SIZE], start):
            lines.append(f"{index+1}. {row['name']}\nКонтакт {row['contact_id']} · сделка {row['lead_id']}\nВоронка {row['pipeline_id']} · статус {row['status_id']}" + (f"\nСходство: {row['score']:.0%}" if row.get("score") else ""))
            buttons.append([link_button("Контакт CRM", f"{self.config.amocrm_base_url}/contacts/detail/{row['contact_id']}"),
                            link_button("Сделка CRM", f"{self.config.amocrm_base_url}/leads/detail/{row['lead_id']}")])
            if review["status"] == "pending":
                buttons.append([button(f"Выбрать №{index+1}", f"admin:choose:{rid}:{review['version']}:{index}")])
        if not candidates:
            lines.append(f"Кандидаты не найдены. После проверки CRM укажите /match {rid} ID_контакта ID_сделки, затем подтвердите кнопкой.")
        if page:
            buttons.append([button("←", f"admin:review:{rid}:{page-1}")])
        if start + PAGE_SIZE < len(candidates):
            buttons.append([button("→", f"admin:review:{rid}:{page+1}")])
        if review["status"] == "pending":
            buttons.append([button("Отклонить", f"admin:reject:{rid}"), button("Перепроверить", f"admin:retry:{sub['id']}")])
        buttons.append([button("↩ К заявкам", "admin:reviews:0")])
        await self.service.say(incoming, "\n\n".join(lines), buttons)

    async def client_page(self, incoming, cid):
        client = await self.db.one("SELECT * FROM clients WHERE id=?", (cid,))
        if not client:
            raise ValueError("Клиент не найден")
        bindings = await self.db.all("SELECT DISTINCT lead_id FROM bindings b JOIN subscriptions s ON s.id=b.subscription_id WHERE s.client_id=?", (cid,))
        buttons = [[link_button("Контакт amoCRM", f"{self.config.amocrm_base_url}/contacts/detail/{client['contact_id']}")]]
        buttons.extend([[link_button(f"Сделка {x['lead_id']}", f"{self.config.amocrm_base_url}/leads/detail/{x['lead_id']}")] for x in bindings[:10]])
        buttons += [[button("Вернуть в active вручную", f"admin:activeask:{cid}")], [button("↩ Подписчики", "admin:subscribers:0")]]
        await self.service.say(incoming, f"◆ {client['name']}\nСегмент: {client['segment']} ({client['segment_source']})\nПодписка: {self.local_date(client['subscribed_at'])}\nКанал: {client['preferred_platform']}\nРассылки: {'отписан' if client['opted_out'] else 'подписан'}", buttons)

    async def manual_candidate(self, incoming):
        if not self.config.admin(incoming.platform, incoming.user_id):
            await self.service.say(incoming, "Доступ запрещён.")
            return
        try:
            _, rid, contact_id, lead_id = incoming.text.split()
            review = await self.db.one("SELECT * FROM reviews WHERE id=? AND status='pending'", (int(rid),))
            if not review:
                raise ValueError("Заявка не найдена")
            contact = await self.service.crm.contact(int(contact_id))
            candidates = await self.service.candidates([contact])
            selected = [x for x in candidates if x["lead_id"] == int(lead_id)]
            if len(selected) != 1:
                raise ValueError("Контакт не связан со сделкой")
            await self.db.execute("UPDATE reviews SET candidates=?,version=version+1 WHERE id=?", (json.dumps(selected, ensure_ascii=False), int(rid)))
            await self.db.event("manual_candidate_added", review["subscription_id"], detail={"actor": f"{incoming.platform}:{incoming.user_id}", "contact_id": int(contact_id), "lead_id": int(lead_id)})
            await self.review_page(incoming, int(rid), 0)
        except (ValueError, KeyError):
            await self.service.say(incoming, "Используйте /match ID_заявки ID_контакта ID_сделки. Проверьте, что заявка ожидает подтверждения и контакт связан со сделкой.")
