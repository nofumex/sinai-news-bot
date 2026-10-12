import json
from datetime import datetime
from zoneinfo import ZoneInfo
from .db import stamp, dumps
from .links import issue, parse_lead, token_hash
from .matching import contact_phones
from .platforms.base import button, link_button

WELCOME = 'Добро пожаловать! Это официальный чат-бот компании „Синай“. Здесь мы будем отправлять вам особенно важные новости'
SEGMENTS = {"active": "Действующий клиент", "completed": "Процедура завершена"}


class Service:
    def __init__(self, config, db, crm, platforms):
        self.config, self.db, self.crm, self.platforms = config, db, crm, platforms
        self.context = ""
        self.sequence = 0
        self.admin = None

    async def say(self, incoming, text, buttons=None, **kwargs):
        self.sequence += 1
        key = f"reply:{self.context}:{self.sequence}"
        await self.db.enqueue(key, "message", {"platform": incoming.platform, "user_id": incoming.user_id,
                             "chat_id": incoming.chat_id, "text": text, "buttons": buttons, **kwargs})

    async def subscription(self, incoming):
        await self.db.execute("INSERT INTO subscriptions(platform,user_id,chat_id,created_at) VALUES(?,?,?,?) "
                              "ON CONFLICT(platform,user_id) DO UPDATE SET chat_id=excluded.chat_id",
                              (incoming.platform, incoming.user_id, incoming.chat_id, stamp()))
        return await self.db.one("SELECT * FROM subscriptions WHERE platform=? AND user_id=?",
                                 (incoming.platform, incoming.user_id))

    async def handle(self, platform, update, context):
        self.context, self.sequence = context, 0
        incoming = self.platforms[platform].parse(update)
        if not incoming:
            return
        if incoming.callback_id:
            # Callback acknowledgements expire; a failure must not replay an admin decision.
            try:
                await self.platforms[platform].answer(incoming.callback_id)
            except Exception:
                pass
        if incoming.text == "/admin" or incoming.callback.startswith("admin:"):
            if self.config.admin(platform, incoming.user_id):
                await self.admin.handle(incoming)
            else:
                await self.say(incoming, "Доступ запрещён.")
            return
        if incoming.text.startswith("/match"):
            await self.admin.manual_candidate(incoming)
            return
        if incoming.text.startswith("/link") or incoming.callback.startswith("link:"):
            if not self.config.manager(platform, incoming.user_id):
                await self.say(incoming, "Доступ запрещён.")
                return
            await self.manager_link(incoming)
            return
        sub = await self.subscription(incoming)
        if incoming.stopped or incoming.text.strip() == "/stop":
            async with self.db.transaction():
                await self.db.execute("UPDATE subscriptions SET stopped=1 WHERE id=?", (sub["id"],))
                if sub["client_id"]:
                    await self.db.execute("UPDATE clients SET opted_out=1 WHERE id=?", (sub["client_id"],))
                    await self.db.execute("UPDATE subscriptions SET stopped=1 WHERE client_id=?", (sub["client_id"],))
                await self.db.event("unsubscribe", sub["id"], sub["client_id"])
            if not incoming.stopped:
                await self.say(incoming, "Вы отписались от рассылок во всех подключённых каналах. Для возврата — /start.", remove_keyboard=True)
            return
        if incoming.text.strip().startswith("/start"):
            payload = incoming.text.strip().split(maxsplit=1)
            token = payload[1] if len(payload) == 2 else ""
            if token:
                async with self.db.transaction():
                    link = await self.db.one("SELECT * FROM links WHERE token_hash=? AND platform=? AND expires_at>? AND consumed_at IS NULL",
                                             (token_hash(token), platform, stamp()))
                    if link and link["claimed_by"] in (None, sub["id"]):
                        await self.db.execute("UPDATE links SET claimed_by=? WHERE token_hash=?", (sub["id"], token_hash(token)))
                        await self.db.execute("UPDATE subscriptions SET deep_token_hash=? WHERE id=?", (token_hash(token), sub["id"]))
                    else:
                        link = None
                if not link:
                    await self.say(incoming, "Ссылка недействительна, истекла или уже использована. Запросите новую у менеджера.")
                    return
            elif not sub["client_id"]:
                await self.db.execute("UPDATE subscriptions SET deep_token_hash=NULL WHERE id=?", (sub["id"],))
            if sub["client_id"] and not token:
                async with self.db.transaction():
                    await self.db.execute("UPDATE subscriptions SET stopped=0,blocked=0 WHERE id=?", (sub["id"],))
                    await self.db.execute("UPDATE clients SET opted_out=0 WHERE id=?", (sub["client_id"],))
                    await self.db.event("resubscribe", sub["id"], sub["client_id"])
                await self.say(incoming, "Ваша подписка возобновлена. /channel — предпочитаемый канал; /stop — отписка.")
                return
            await self.say(incoming, WELCOME, [[button("Я действующий клиент", "segment:active")],
                                             [button("Моя процедура уже завершена", "segment:completed")]])
            return
        if incoming.text.strip() == "/channel" and sub["client_id"]:
            await self.db.execute("UPDATE clients SET preferred_platform=? WHERE id=?", (platform, sub["client_id"]))
            await self.db.event("channel_changed", sub["id"], sub["client_id"], {"platform": platform})
            await self.say(incoming, "Этот мессенджер выбран для рассылок.")
            return
        if incoming.callback in {"segment:active", "segment:completed"}:
            if sub["client_id"] and not sub["deep_token_hash"]:
                await self.say(incoming, "Вы уже зарегистрированы. Сегмент определяется по CRM.")
                return
            await self.db.execute("UPDATE subscriptions SET selected_segment=?,state='contact' WHERE id=?",
                                 (incoming.callback.split(":")[1], sub["id"]))
            await self.say(incoming, "Для регистрации поделитесь своим контактом кнопкой ниже.", request_contact=True)
            return
        if incoming.contact:
            if not sub["selected_segment"]:
                await self.say(incoming, "Сначала выберите статус: /start.")
                return
            try:
                number = self.platforms[platform].verify_contact(incoming.contact, incoming.user_id)
            except (ValueError, KeyError, TypeError):
                await self.say(incoming, "Не удалось подтвердить ваш контакт. Отправьте собственный контакт кнопкой ниже.", request_contact=True)
                return
            if sub["client_id"] and number == sub["phone"] and sub["state"] == "confirmed":
                await self.say(incoming, "Вы уже зарегистрированы.", remove_keyboard=True)
                return
            async with self.db.transaction():
                await self.db.execute("UPDATE subscriptions SET phone=?,subscribed_at=COALESCE(subscribed_at,?),state='syncing',stopped=0,blocked=0 WHERE id=?",
                                      (number, stamp(), sub["id"]))
                await self.db.event("contact_verified", sub["id"], sub["client_id"], {"method": platform + "_contact"})
                await self.db.enqueue(f"sync:{context}:{sub['id']}", "sync", {"subscription_id": sub["id"]})
            await self.say(incoming, "Контакт подтверждён. Проверяем данные в CRM.", remove_keyboard=True)
            return
        if sub["state"] == "name" and incoming.text and not incoming.text.startswith("/"):
            if not 3 <= len(incoming.text.strip()) <= 150:
                await self.say(incoming, "Укажите фамилию, имя и отчество (до 150 символов).")
                return
            await self.db.execute("UPDATE subscriptions SET name=?,state='syncing' WHERE id=?", (incoming.text.strip(), sub["id"]))
            await self.db.enqueue(f"name:{context}:{sub['id']}", "sync", {"subscription_id": sub["id"], "by_name": True})
            await self.say(incoming, "Ищем контакты по ФИО. Результат подтвердит администратор.")
            return
        await self.say(incoming, "Регистрация — /start. Отписка — /stop. Выбор канала рассылок — /channel.")

    async def tell_sub(self, sub, text):
        from .platforms.base import Incoming
        await self.say(Incoming(sub["platform"], sub["user_id"], sub["chat_id"]), text)

    async def candidates(self, contacts):
        result = []
        for item in contacts:
            contact = await self.crm.contact(item["id"])
            leads = await self.crm.leads_for(contact)
            for lead in leads:
                result.append({"contact_id": contact["id"], "lead_id": lead["id"], "name": contact.get("name", ""),
                               "pipeline_id": lead["pipeline_id"], "status_id": lead["status_id"],
                               "score": item.get("match_score"), "phone_match": False})
        return result

    async def sync(self, sub_id, by_name=False):
        sub = await self.db.one("SELECT * FROM subscriptions WHERE id=?", (sub_id,))
        if not sub or not sub["phone"] or sub["stopped"]:
            return
        if sub["state"] == "confirmed":
            return
        if sub["deep_token_hash"]:
            link = await self.db.one("SELECT * FROM links WHERE token_hash=?", (sub["deep_token_hash"],))
            if not link or link["expires_at"] <= stamp() or link["claimed_by"] != sub_id or link["consumed_at"]:
                await self.review(sub, [], "Персональная ссылка истекла или недействительна")
                return
            contact = await self.crm.contact(link["contact_id"])
            lead_ids = {x["id"] for x in contact.get("_embedded", {}).get("leads", [])}
            lead = await self.crm.lead(link["lead_id"])
            candidate = {"contact_id": contact["id"], "lead_id": lead["id"], "name": contact.get("name", ""),
                         "pipeline_id": lead["pipeline_id"], "status_id": lead["status_id"]}
            exact = await self.crm.by_phone(sub["phone"])
            if sub["phone"] in contact_phones(contact) and link["lead_id"] in lead_ids and {x['id'] for x in exact} == {contact['id']}:
                await self.bind(sub, candidate, "deeplink_phone")
            else:
                await self.review(sub, [candidate], "Конфликт телефона, связи сделки или неоднозначный номер в персональной ссылке")
            return
        contacts = await (self.crm.by_name(sub["name"]) if by_name else self.crm.by_phone(sub["phone"]))
        if not contacts and not by_name:
            await self.db.execute("UPDATE subscriptions SET state='name' WHERE id=?", (sub_id,))
            await self.db.event("phone_not_found", sub_id)
            await self.tell_sub(sub, "Номер не найден в CRM. Напишите, пожалуйста, фамилию, имя и отчество полностью.")
            return
        candidates = await self.candidates(contacts)
        if not by_name and len(contacts) == 1 and len(candidates) == 1:
            await self.bind(sub, candidates[0], "phone")
        else:
            reason = "Совпадение только по ФИО" if by_name and candidates else "Неоднозначная связь контакта и сделки"
            await self.review(sub, candidates, reason if candidates else "Клиент или связанные сделки не найдены в CRM")

    async def review(self, sub, candidates, reason):
        async with self.db.transaction():
            await self.db.execute("UPDATE subscriptions SET state=? WHERE id=?", ("review" if candidates else "not_found", sub["id"]))
            await self.db.execute("INSERT INTO reviews(subscription_id,reason,candidates,created_at) VALUES(?,?,?,?) "
                                  "ON CONFLICT(subscription_id) WHERE status='pending' DO UPDATE SET reason=excluded.reason,candidates=excluded.candidates,version=reviews.version+1",
                                  (sub["id"], reason, dumps(candidates), stamp()))
            review = await self.db.one("SELECT id FROM reviews WHERE subscription_id=? AND status='pending'", (sub["id"],))
            await self.db.event("review_requested", sub["id"], detail={"reason": reason})
        await self.tell_sub(sub, "Данные переданы администратору для проверки. После подтверждения мы сообщим вам.")
        from .platforms.base import Incoming
        for platform, uid, token in [("telegram", self.config.admin_id, self.config.tg_bot_token),
                                     ("max", self.config.max_admin_id, self.config.max_bot_token)]:
            if uid and token.get_secret_value():
                await self.say(Incoming(platform, uid, uid), f"🔎 Новая заявка №{review['id']}\n{sub.get('name') or 'Контакт подтверждён'}\n{reason}",
                               [[button("Открыть заявку", f"admin:review:{review['id']}:0")]])

    async def bind(self, sub, candidate, method, actor=None, review_id=None):
        contact = await self.crm.contact(candidate["contact_id"])
        if sub["client_id"] and method != "manual":
            previous = await self.db.one("SELECT contact_id FROM clients WHERE id=?", (sub["client_id"],))
            if previous and previous["contact_id"] != contact["id"]:
                await self.review(sub, [candidate], "Попытка изменить ранее подтверждённый контакт CRM")
                return
        if candidate["lead_id"] not in {x["id"] for x in contact.get("_embedded", {}).get("leads", [])}:
            raise ValueError("Контакт больше не связан с указанной сделкой. Повторите проверку.")
        if method != "manual" and sub["phone"] not in contact_phones(contact):
            raise ValueError("Телефон в CRM изменился; требуется ручная проверка.")
        all_leads = await self.crm.leads_for(contact)
        lead = next(x for x in all_leads if x["id"] == candidate["lead_id"])
        completed = any(x["pipeline_id"] == self.config.completed_pipeline_id for x in all_leads)
        segment = "completed" if completed else (sub["selected_segment"] or "active")
        async with self.db.transaction():
            if review_id:
                review = await self.db.one("SELECT * FROM reviews WHERE id=? AND status='pending'", (review_id,))
                if not review or review["subscription_id"] != sub["id"]:
                    return
            existing = await self.db.one("SELECT * FROM clients WHERE contact_id=?", (contact["id"],))
            if existing:
                client_id = existing["id"]
                anchor = min(existing["subscribed_at"], sub["subscribed_at"])
                segment = "completed" if existing["segment"] == "completed" else segment
                await self.db.execute("UPDATE clients SET name=?,subscribed_at=?,segment=?,opted_out=0 WHERE id=?",
                                      (contact.get("name", ""), anchor, segment, client_id))
            else:
                cursor = await self.db.execute("INSERT INTO clients(contact_id,name,segment,subscribed_at,preferred_platform,segment_source,created_at) VALUES(?,?,?,?,?,?,?)",
                                               (contact["id"], contact.get("name", ""), segment, sub["subscribed_at"],
                                                self.config.preferred_platform, "crm" if completed else "selection", stamp()))
                client_id = cursor.lastrowid
            await self.db.execute("UPDATE subscriptions SET client_id=?,name=?,state='confirmed',stopped=0,blocked=0 WHERE id=?",
                                  (client_id, contact.get("name", ""), sub["id"]))
            replaced = await self.db.all("SELECT contact_id,lead_id,method FROM bindings WHERE subscription_id=? AND (contact_id<>? OR lead_id<>?)",
                                         (sub["id"], contact["id"], lead["id"]))
            if replaced:
                await self.db.event("bindings_replaced", sub["id"], client_id, {"previous": replaced, "actor": actor, "method": method})
                await self.db.execute("DELETE FROM bindings WHERE subscription_id=? AND (contact_id<>? OR lead_id<>?)",
                                      (sub["id"], contact["id"], lead["id"]))
            await self.db.execute("INSERT INTO bindings(subscription_id,contact_id,lead_id,method,verified_by,created_at,crm_phones) VALUES(?,?,?,?,?,?,?) "
                                  "ON CONFLICT(subscription_id,contact_id,lead_id) DO UPDATE SET method=excluded.method,verified_by=excluded.verified_by,crm_phones=excluded.crm_phones",
                                  (sub["id"], contact["id"], lead["id"], method, actor, stamp(), dumps(sorted(contact_phones(contact)))))
            for item in all_leads:
                await self.cache_lead(item)
            await self.db.execute("UPDATE clients SET last_crm_check=? WHERE id=?", (stamp(), client_id))
            if review_id:
                await self.db.execute("UPDATE reviews SET status='confirmed',resolved_at=?,resolved_by=? WHERE id=?",
                                      (stamp(), actor, review_id))
            else:
                await self.db.execute("UPDATE reviews SET status='superseded',resolved_at=? WHERE subscription_id=? AND status='pending'", (stamp(), sub["id"]))
            if sub["deep_token_hash"]:
                await self.db.execute("UPDATE links SET consumed_at=? WHERE token_hash=?", (stamp(), sub["deep_token_hash"]))
                await self.db.execute("UPDATE subscriptions SET deep_token_hash=NULL WHERE id=?", (sub["id"],))
            key = f"subscription-{sub['id']}-contact-{contact['id']}-lead-{lead['id']}"
            date = datetime.fromisoformat(sub["subscribed_at"]).astimezone(ZoneInfo("Asia/Krasnoyarsk")).strftime("%d.%m.%Y %H:%M")
            text = f"Клиент подписался на официальный бот Синай | {'Telegram' if sub['platform']=='telegram' else 'MAX'} | {date} | {SEGMENTS[segment]} | Контакт CRM: {contact['id']}"
            await self.db.execute("INSERT OR IGNORE INTO crm_notes(key,lead_id,text,created_at) VALUES(?,?,?,?)", (key, lead["id"], text, stamp()))
            await self.db.enqueue("note:" + key, "note", {"key": key})
            await self.db.event("matched_manual" if method == "manual" else "matched_auto", sub["id"], client_id, {"method": method, "lead_id": lead["id"], "actor": actor})
            await self.tell_sub(sub, "Регистрация завершена. Вы подписаны на новости компании «Синай». /stop — отписка; /channel — выбрать этот канал для рассылок.")

    async def cache_lead(self, lead):
        await self.db.execute("INSERT INTO leads VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET pipeline_id=excluded.pipeline_id,status_id=excluded.status_id,checked_at=excluded.checked_at",
                              (lead["id"], lead["pipeline_id"], lead["status_id"], stamp()))

    async def reconcile_client(self, client_id):
        client = await self.db.one("SELECT * FROM clients WHERE id=?", (client_id,))
        if not client:
            return
        contact = await self.crm.contact(client["contact_id"])
        leads = await self.crm.leads_for(contact)
        linked = {x["id"] for x in leads}
        bindings = await self.db.all("SELECT b.*,s.* FROM bindings b JOIN subscriptions s ON s.id=b.subscription_id WHERE s.client_id=?", (client_id,))
        # Missing/changed relationships pause sending and go to manual review.
        for sub in bindings:
            verified_override = sub["method"] == "manual" and json.loads(sub["crm_phones"]) == sorted(contact_phones(contact))
            if sub["lead_id"] not in linked or (sub["phone"] not in contact_phones(contact) and not verified_override):
                if sub["state"] == "confirmed":
                    await self.review(sub, await self.candidates([contact]), "В CRM изменился телефон или связь со сделкой")
        async with self.db.transaction():
            for lead in leads:
                await self.cache_lead(lead)
            if any(x["pipeline_id"] == self.config.completed_pipeline_id for x in leads) and client["segment"] != "completed":
                await self.db.execute("UPDATE clients SET segment='completed',segment_source='crm' WHERE id=?", (client_id,))
                await self.db.event("segment_completed", client_id=client_id)
            # Completed is sticky. Returning to active requires an explicit admin decision.
            await self.db.execute("UPDATE clients SET name=?,last_crm_check=? WHERE id=?", (contact.get("name", ""), stamp(), client_id))

    async def reconcile_lead(self, lead_id):
        # Always read the actual card, never trust pipeline/status from a webhook.
        lead = await self.crm.lead(lead_id)
        await self.cache_lead(lead)
        ids = {x["id"] for x in lead.get("_embedded", {}).get("contacts", [])}
        clients = await self.db.all("SELECT id,contact_id FROM clients")
        for client in clients:
            if client["contact_id"] in ids or await self.db.one("SELECT 1 FROM bindings b JOIN subscriptions s ON s.id=b.subscription_id WHERE b.lead_id=? AND s.client_id=?", (lead_id, client["id"])):
                await self.reconcile_client(client["id"])

    async def manager_link(self, incoming):
        try:
            if incoming.callback:
                _, lead_id, contact_id = incoming.callback.split(":")
                lead_id, contact_id = int(lead_id), int(contact_id)
            else:
                parts = incoming.text.split()
                if len(parts) not in (2, 3):
                    raise ValueError("Используйте /link ID_сделки [ID_контакта] или /link URL_сделки.")
                lead_id = parse_lead(parts[1], self.config.amocrm_base_url)
                contact_id = int(parts[2]) if len(parts) == 3 else None
            lead = await self.crm.lead(lead_id)
            contacts = lead.get("_embedded", {}).get("contacts", [])
            if contact_id is None and len(contacts) == 1:
                contact_id = contacts[0]["id"]
            if contact_id is None:
                await self.say(incoming, "У сделки несколько контактов или контакты отсутствуют. Укажите /link ID_сделки ID_контакта.\n" +
                               "\n".join(f"Контакт {x['id']}: {self.config.amocrm_base_url}/contacts/detail/{x['id']}" for x in contacts[:15]))
                return
            if contact_id not in {x["id"] for x in contacts}:
                raise ValueError("Указанный контакт не связан со сделкой.")
            contact = await self.crm.contact(contact_id)
            actor = f"{incoming.platform}:{incoming.user_id}"
            urls = []
            async with self.db.transaction():
                for platform, username in [("telegram", self.config.tg_bot_username), ("max", self.config.max_bot_username)]:
                    if username:
                        urls.append((platform, await issue(self.db, self.config, platform, lead_id, contact_id, actor)))
                await self.db.event("link_issued", detail={"actor": actor, "lead_id": lead_id, "contact_id": contact_id})
            await self.say(incoming, f"🔗 {contact.get('name','')}\nСделка {lead_id}, контакт {contact_id}\nОдноразовые ссылки действуют {self.config.link_ttl_seconds//3600} ч.\n" +
                           "\n".join(f"{p}: {url}" for p, url in urls) + ("\nMAX: заполните MAX_BOT_USERNAME." if not self.config.max_bot_username else ""),
                           [[link_button("Открыть сделку", f"{self.config.amocrm_base_url}/leads/detail/{lead_id}")]])
        except (ValueError, KeyError) as exc:
            await self.say(incoming, str(exc))
