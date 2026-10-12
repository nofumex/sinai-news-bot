from datetime import datetime
from zoneinfo import ZoneInfo
from dateutil.relativedelta import relativedelta
from .db import now, stamp
from .http import APIError, AmbiguousSend, RetryLater

TZ = ZoneInfo("Asia/Krasnoyarsk")
ACTIVE_TEXT = "Напоминаем о нашей бонусной программе. За приведённого клиента вы получите скидку к оплате 8000 рублей. Если вы уже рассчитались, мы переведём вам эту сумму на карту. А ваш знакомый получит скидку 5000 рублей"
COMPLETED_TEXT = "Напоминаем вам о бонусной программе. За каждого приведённого клиента вы получите 8000 рублей, а он получит скидку 5000 рублей"


def anniversary(anchor: datetime, month: int) -> datetime:
    # Every date is calculated directly from the original anchor (Jan 31 -> Feb 28 -> Mar 31).
    return anchor.astimezone(TZ) + relativedelta(months=month)


def latest_cycle(anchor: datetime, current: datetime, interval: int) -> int:
    local = current.astimezone(TZ)
    start = anchor.astimezone(TZ)
    months = (local.year - start.year) * 12 + local.month - start.month
    cycle = max(0, months // interval * interval)
    if cycle and anniversary(anchor, cycle) > local:
        cycle -= interval
    return max(0, cycle)


class Scheduler:
    def __init__(self, config, db, service):
        self.config, self.db, self.service = config, db, service

    async def interval(self, segment):
        fallback = self.config.active_interval_months if segment == "active" else self.config.completed_interval_months
        return int(await self.db.setting(segment + "_interval_months", fallback))

    async def channel(self, client):
        subscriptions = await self.db.all("SELECT * FROM subscriptions WHERE client_id=? AND stopped=0 AND blocked=0 AND state IN ('confirmed','registered') ORDER BY id",
                                          (client["id"],))
        available = [s for s in subscriptions if (self.config.tg_bot_token if s["platform"] == "telegram" else self.config.max_bot_token).get_secret_value()]
        return next((s for s in available if s["platform"] == client["preferred_platform"]), available[0] if available else None)

    async def plan(self, current=None):
        current = current or now()
        result = []
        for client in await self.db.all("SELECT * FROM clients WHERE opted_out=0 AND merged_into IS NULL ORDER BY id"):
            sub = await self.channel(client)
            if not sub:
                continue
            interval = await self.interval(client["segment"])
            anchor = datetime.fromisoformat(client["subscribed_at"])
            cycle = latest_cycle(anchor, current, interval)
            family = await self.db.family(client['id'])
            marks = ','.join('?' for _ in family)
            last = await self.db.one(f"SELECT MAX(cycle) AS cycle FROM deliveries WHERE client_id IN ({marks})", family)
            previous = last["cycle"] or 0
            next_cycle = cycle if cycle > previous else ((max(cycle, previous) // interval) + 1) * interval
            result.append({"client_id": client["id"], "name": client["name"], "subscription_id": sub["id"],
                           "platform": sub["platform"], "cycle": next_cycle, "segment": client["segment"],
                           "due_at": stamp(anniversary(anchor, next_cycle)), "due": next_cycle == cycle and cycle > 0})
        return sorted(result, key=lambda x: (x["due_at"], x["client_id"]))

    async def tick(self, current=None):
        if self.config.test_mode or not self.config.broadcasts_enabled:
            return
        for item in await self.plan(current):
            if not item["due"]:
                continue
            text = COMPLETED_TEXT if item["segment"] == "completed" else ACTIVE_TEXT
            async with self.db.transaction():
                await self.db.execute("INSERT OR IGNORE INTO deliveries(client_id,subscription_id,cycle,segment,text,due_at,created_at) VALUES(?,?,?,?,?,?,?)",
                                      (item["client_id"], item["subscription_id"], item["cycle"], item["segment"], text, item["due_at"], stamp()))
                row = await self.db.one("SELECT id FROM deliveries WHERE client_id=? AND cycle=?", (item["client_id"], item["cycle"]))
                await self.db.enqueue(f"broadcast:{row['id']}", "broadcast", {"delivery_id": row["id"]})

    async def deliver(self, delivery_id):
        row = await self.db.one("SELECT * FROM deliveries WHERE id=?", (delivery_id,))
        if not row or row["state"] not in {"pending", "retry"}:
            return
        if self.config.test_mode or not self.config.broadcasts_enabled:
            raise RetryLater("broadcasts_disabled", 3600)
        bound = await self.db.one('SELECT 1 FROM subscriptions WHERE client_id=? AND claimed_lead_id IS NOT NULL', ((await self.db.canonical(row['client_id']))['id'],))
        if bound:
            await self.service.crm.verify_pipeline()
        # Fresh CRM check before each send. A CRM outage prevents sending the wrong segment.
        await self.service.reconcile_client(row["client_id"])
        client = await self.db.canonical(row['client_id'])
        sub = await self.channel(client)
        interval = await self.interval(client["segment"])
        cycle = latest_cycle(datetime.fromisoformat(client["subscribed_at"]), now(), interval)
        family = await self.db.family(client['id'])
        marks = ','.join('?' for _ in family)
        duplicate = await self.db.one(f"SELECT 1 FROM deliveries WHERE client_id IN ({marks}) AND id<>? AND state IN ('sent','sending','unknown') AND COALESCE(sent_at,created_at)>=?", (*family, delivery_id, stamp(anniversary(datetime.fromisoformat(client['subscribed_at']), cycle))))
        if client["opted_out"] or not sub or row["cycle"] != cycle or duplicate:
            await self.db.execute("UPDATE deliveries SET state='skipped',error='unsubscribed/no_channel/stale_cycle' WHERE id=?", (delivery_id,))
            return
        text = COMPLETED_TEXT if client["segment"] == "completed" else ACTIVE_TEXT
        await self.db.execute("UPDATE deliveries SET state='sending',subscription_id=?,segment=?,text=? WHERE id=?",
                              (sub["id"], client["segment"], text, delivery_id))
        try:
            external_id = await self.service.platforms[sub["platform"]].send(sub["user_id"], sub["chat_id"], text)
        except AmbiguousSend as exc:
            await self.db.execute("UPDATE deliveries SET state='unknown',error=? WHERE id=?", (str(exc), delivery_id))
            await self.db.event("send_unknown", sub["id"], client["id"], {"delivery_id": delivery_id})
            raise
        except APIError as exc:
            blocked = exc.status == 403
            await self.db.execute("UPDATE deliveries SET state=?,error=? WHERE id=?", ("blocked" if blocked else "retry", str(exc), delivery_id))
            await self.db.event("send_error", sub["id"], client["id"], {"delivery_id": delivery_id, "error": str(exc)})
            if blocked:
                await self.db.execute("UPDATE subscriptions SET blocked=1 WHERE id=?", (sub["id"],))
                replacement = await self.channel(client)
                if replacement:
                    await self.db.execute("UPDATE deliveries SET state='retry' WHERE id=?", (delivery_id,))
                    raise RetryLater("channel_blocked_fallback", 5) from None
                return
            raise
        async with self.db.transaction():
            await self.db.execute("UPDATE deliveries SET state='sent',sent_at=?,external_id=?,error=NULL WHERE id=?",
                                  (stamp(), external_id, delivery_id))
            await self.db.event("send_success", sub["id"], client["id"], {"delivery_id": delivery_id})
