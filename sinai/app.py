import asyncio
import hashlib
import hmac
import json
import logging
import re
from contextlib import asynccontextmanager, suppress
from urllib.parse import parse_qsl
from fastapi import FastAPI, Request, HTTPException
from filelock import FileLock
from .admin import Admin
from .config import load_settings
from .crm import CRM
from .db import Database, dumps
from .platforms import Telegram, Max
from .scheduler import Scheduler
from .service import Service
from .worker import Worker


def configure_logs(level):
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # HTTP client URLs may contain credentials; never log them.
    for name in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def event_key(platform, update):
    if platform == "telegram" and "update_id" in update:
        return f"telegram:{update['update_id']}"
    if platform == "max":
        callback_id = (update.get("callback") or {}).get("callback_id")
        mid = ((update.get("message") or {}).get("body") or {}).get("mid")
        if callback_id:
            return "max:callback:" + str(callback_id)
        if mid:
            return f"max:{update.get('update_type')}:{mid}"
    return platform + ":" + hashlib.sha256(dumps(update).encode()).hexdigest()


def amo_lead_ids(body, content_type):
    ids = set()
    if "application/json" in content_type:
        obj = json.loads(body)
        for action in ("status", "update"):
            values = obj.get("leads", {}).get(action, [])
            if isinstance(values, dict):
                values = values.values()
            for value in values:
                if str(value.get("id", "")).isdigit():
                    ids.add(int(value["id"]))
    else:
        for key, value in parse_qsl(body.decode(), max_num_fields=5000):
            if re.fullmatch(r"leads\[(?:status|update)\]\[\d+\]\[id\]", key) and value.isdigit():
                ids.add(int(value))
    return sorted(ids)


def create_app(config=None):
    config = config or load_settings()
    configure_logs(config.log_level)
    db = Database(config.database_path)
    crm = CRM(config, db)
    platforms = {"telegram": Telegram(config), "max": Max(config)}
    service = Service(config, db, crm, platforms)
    scheduler = Scheduler(config, db, service)
    service.admin = Admin(config, db, service, scheduler)
    worker = Worker(config, db, service, scheduler)

    @asynccontextmanager
    async def lifespan(app):
        config.database_path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite mode deliberately runs exactly one process. A second process fails closed.
        lock = FileLock(str(config.database_path) + ".process.lock")
        lock.acquire(timeout=0)
        task = None
        try:
            await db.open()
            task = asyncio.create_task(worker.run(), name="sinai-worker")
            yield
        finally:
            worker.stopping.set()
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await crm.http.close()
            for platform in platforms.values():
                await platform.http.close()
            await db.close()
            lock.release()

    app = FastAPI(title="Синай · CRM subscriptions", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db, app.state.service, app.state.scheduler, app.state.worker = db, service, scheduler, worker

    async def read_body(request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 262144:
                raise HTTPException(413, "payload too large")
        return bytes(data)

    def check_secret(received, expected):
        if not expected or not hmac.compare_digest(received or "", expected):
            raise HTTPException(403, "forbidden")

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        if not db.conn:
            raise HTTPException(503, "database unavailable")
        await db.one("SELECT 1")
        if not task_alive():
            raise HTTPException(503, "worker unavailable")
        return {"status": "ok", "test_mode": config.test_mode, "broadcasts_enabled": config.broadcasts_enabled}

    def task_alive():
        return any(t.get_name() == "sinai-worker" and not t.done() for t in asyncio.all_tasks())

    @app.post("/webhooks/telegram")
    async def telegram(request: Request):
        check_secret(request.headers.get("X-Telegram-Bot-Api-Secret-Token"), config.tg_webhook_secret.get_secret_value())
        return await accept("telegram", request)

    @app.post("/webhooks/max")
    async def max_hook(request: Request):
        check_secret(request.headers.get("X-Max-Bot-Api-Secret"), config.max_webhook_secret.get_secret_value())
        return await accept("max", request)

    async def accept(platform, request):
        try:
            update = json.loads(await read_body(request))
            if not isinstance(update, dict):
                raise ValueError()
            key = event_key(platform, update)
            async with db.transaction():
                await db.enqueue(key, "inbox", {"platform": platform, "update": update})
                # Apply opt-out immediately, even if a slow CRM job is currently running.
                incoming = platforms[platform].parse(update)
                if incoming and (incoming.stopped or incoming.text.strip() == "/stop"):
                    sub = await db.one("SELECT * FROM subscriptions WHERE platform=? AND user_id=?", (platform, incoming.user_id))
                    if sub:
                        await db.execute("UPDATE subscriptions SET stopped=1 WHERE id=?", (sub["id"],))
                        if sub["client_id"]:
                            await db.execute("UPDATE clients SET opted_out=1 WHERE id=?", (sub["client_id"],))
                            await db.execute("UPDATE subscriptions SET stopped=1 WHERE client_id=?", (sub["client_id"],))
        except (ValueError, TypeError, AttributeError, KeyError):
            raise HTTPException(400, "invalid update") from None
        return {"ok": True}

    @app.post("/webhooks/amo/{secret}")
    async def amo(request: Request, secret: str):
        # amoCRM doesn't document an HMAC header. Use a high entropy secret URL,
        # then read every affected lead from the API. Disable reverse proxy URL logs.
        check_secret(secret, config.amo_webhook_secret.get_secret_value())
        body = await read_body(request)
        try:
            ids = amo_lead_ids(body, request.headers.get("content-type", ""))
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
            raise HTTPException(400, "invalid webhook") from None
        digest = hashlib.sha256(body).hexdigest()
        async with db.transaction():
            for lead_id in ids:
                await db.enqueue(f"amo:{digest}:{lead_id}", "reconcile_lead", {"lead_id": lead_id})
        return {"ok": True}

    return app
