import asyncio
import json
import logging
import random
import time
from datetime import timedelta
from .db import now, stamp
from .http import APIError, AmbiguousSend, RetryLater

log = logging.getLogger("sinai.worker")


class Worker:
    def __init__(self, config, db, service, scheduler):
        self.config, self.db, self.service, self.scheduler = config, db, service, scheduler
        self.stopping = asyncio.Event()
        self.heartbeat = time.monotonic()
        self.next_tick = 0.0
        self.next_reconcile = 0.0

    async def recover(self):
        async with self.db.transaction():
            await self.db.execute("UPDATE deliveries SET state='unknown',error='process_interrupted_during_send' WHERE state='sending'")
            # A crashed message send may have succeeded. Do not automatically repeat it.
            await self.db.execute("UPDATE jobs SET state='unknown',last_error='process_interrupted' WHERE state='running' AND kind='message'")
            await self.db.execute("UPDATE jobs SET state='pending',available_at=? WHERE state='running'", (stamp(),))
            # Any pending note with a running job could have reached CRM: check, never blind-write.
            await self.db.execute("UPDATE crm_notes SET state='unknown' WHERE key IN (SELECT json_extract(payload,'$.key') FROM jobs WHERE kind='note' AND attempts>0 AND state='pending') AND state='pending'")

    async def periodic(self):
        current = time.monotonic()
        if current >= self.next_reconcile:
            bucket = int(now().timestamp()) // self.config.reconciliation_seconds
            async with self.db.transaction():
                await self.db.enqueue(f"pipeline:{bucket}", "pipeline", {})
                for client in await self.db.all("SELECT id FROM clients"):
                    await self.db.enqueue(f"reconcile:{bucket}:{client['id']}", "reconcile_client", {"client_id": client["id"]})
            self.next_reconcile = current + self.config.reconciliation_seconds
        if current >= self.next_tick:
            await self.scheduler.tick()
            self.next_tick = current + 30

    async def run(self):
        await self.recover()
        while not self.stopping.is_set():
            self.heartbeat = time.monotonic()
            try:
                await self.periodic()
                worked = await self.run_one()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("worker loop failed: %s", type(exc).__name__)
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(self.stopping.wait(), timeout=1)
                except TimeoutError:
                    pass

    async def run_one(self):
        async with self.db.transaction():
            job = await self.db.one("SELECT * FROM jobs WHERE state='pending' AND available_at<=? ORDER BY CASE WHEN kind='inbox' THEN 0 WHEN kind='message' THEN 1 ELSE 2 END,id LIMIT 1", (stamp(),))
            if not job:
                return False
            await self.db.execute("UPDATE jobs SET state='running',attempts=attempts+1 WHERE id=?", (job["id"],))
        payload = json.loads(job["payload"])
        self.service.context, self.service.sequence = str(job["id"]), 0
        try:
            await self.dispatch(job["kind"], payload, str(job["id"]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = str(exc) if isinstance(exc, APIError) else type(exc).__name__
            log.warning("job %d %s failed: %s", job["id"], job["kind"], message)
            unknown = isinstance(exc, AmbiguousSend) and job["kind"] in {"message", "broadcast"}
            attempt = job["attempts"] + 1
            permanent = isinstance(exc, (ValueError, KeyError, TypeError)) or (isinstance(exc, APIError) and exc.status in {400, 403, 404})
            state = "unknown" if unknown else "failed" if permanent or attempt >= 10 else "pending"
            delay = exc.seconds if isinstance(exc, RetryLater) else min(3600, 2 ** min(attempt, 10) * 10 + random.random() * 5)
            if isinstance(exc, AmbiguousSend) and job["kind"] == "note":
                await self.db.execute("UPDATE crm_notes SET state='unknown' WHERE key=?", (payload["key"],))
                delay = 600
            async with self.db.transaction():
                await self.db.execute("UPDATE jobs SET state=?,available_at=?,last_error=? WHERE id=?", (state, stamp(now()+timedelta(seconds=delay)), message, job["id"]))
                await self.db.execute("INSERT INTO errors(job_id,kind,message,created_at) VALUES(?,?,?,?)", (job["id"], job["kind"], message, stamp()))
                if job["kind"] == "broadcast" and state == "failed":
                    await self.db.execute("UPDATE deliveries SET state='failed',error=? WHERE id=? AND state NOT IN ('sent','unknown')", (message, payload["delivery_id"]))
                if job["kind"] == "message" and isinstance(exc, APIError) and exc.status == 403:
                    await self.db.execute("UPDATE subscriptions SET blocked=1 WHERE platform=? AND user_id=?", (payload["platform"], payload["user_id"]))
            return True
        async with self.db.transaction():
            await self.db.execute("UPDATE jobs SET state='done',last_error=NULL WHERE id=?", (job["id"],))
            await self.db.execute("UPDATE errors SET resolved_at=? WHERE job_id=? AND resolved_at IS NULL", (stamp(), job["id"]))
        return True

    async def dispatch(self, kind, payload, context):
        if kind == "inbox":
            await self.service.handle(payload["platform"], payload["update"], context)
        elif kind == "message":
            platform = payload["platform"]
            args = {k: v for k, v in payload.items() if k != "platform"}
            external_id = await self.service.platforms[platform].send(**args)
            await self.db.event("message_test" if self.config.test_mode else "message_success", detail={"platform": platform, "external_id": external_id})
        elif kind == "sync":
            await self.service.sync(payload["subscription_id"], payload.get("by_name", False))
        elif kind == "reconcile_client":
            await self.service.reconcile_client(payload["client_id"])
        elif kind == "reconcile_lead":
            await self.service.reconcile_lead(payload["lead_id"])
        elif kind == "note":
            await self.service.crm.write_note(payload["key"])
        elif kind == "broadcast":
            await self.scheduler.deliver(payload["delivery_id"])
        elif kind == "pipeline":
            await self.service.crm.verify_pipeline()
        else:
            raise ValueError("Unsupported job type")
