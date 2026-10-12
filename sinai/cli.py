import argparse
import asyncio
import json
import os
import sqlite3
from datetime import timedelta
from pathlib import Path
import uvicorn
from filelock import FileLock
from .config import load_settings
from .crm import CRM
from .db import Database, now, stamp
from .platforms import Telegram, Max
from .scheduler import Scheduler
from .service import Service


async def run_command(command):
    config = load_settings()
    config.database_path.parent.mkdir(parents=True, exist_ok=True)
    # Setup/replay tools share the runtime lock; never race an active bot.
    with FileLock(str(config.database_path) + ".process.lock", timeout=0):
        db = Database(config.database_path)
        await db.open()
        crm = CRM(config, db)
        tg, max_bot = Telegram(config), Max(config)
        try:
            if command == "doctor":
                data = await crm.verify_pipeline()
                print(json.dumps({"pipeline_id": data['id'], "name": data['name'], "test_mode": config.test_mode,
                                  "broadcasts_enabled": config.broadcasts_enabled}, ensure_ascii=True))
                if config.tg_bot_token.get_secret_value():
                    info = await tg.call("getMe", {}, safe=True)
                    print("Telegram username:", info['username'])
                if config.max_bot_token.get_secret_value():
                    info = await max_bot.call("GET", "/me", safe=True)
                    print("MAX username:", info.get('username'))
            elif command == "setup-webhooks":
                if config.test_mode or not config.public_base_url.startswith("https://"):
                    raise ValueError("Set TEST_MODE=false and HTTPS PUBLIC_BASE_URL before webhook setup")
                base = config.public_base_url.rstrip("/")
                if config.tg_bot_token.get_secret_value():
                    await tg.call("setWebhook", {"url": base + "/webhooks/telegram", "secret_token": config.tg_webhook_secret.get_secret_value(),
                                                "allowed_updates": ["message", "callback_query", "my_chat_member"], "drop_pending_updates": False}, safe=True)
                    print("Telegram webhook registered")
                if config.max_bot_token.get_secret_value():
                    await max_bot.call("POST", "/subscriptions", json={"url": base + "/webhooks/max", "secret": config.max_webhook_secret.get_secret_value(),
                                        "update_types": ["bot_started", "bot_stopped", "message_created", "message_callback"]}, safe=True)
                    print("MAX webhook registered")
                # CRM configuration is deliberately explicit; no changes to existing hooks.
                print("amoCRM: configure status_lead, update_lead with PUBLIC_BASE_URL + /webhooks/amo/ + AMO_WEBHOOK_SECRET")
            elif command == "plan":
                service = Service(config, db, crm, {"telegram": tg, "max": max_bot})
                print(json.dumps(await Scheduler(config, db, service).plan(), ensure_ascii=True, indent=2))
            elif command == "reconcile":
                for row in await db.all("SELECT id FROM clients"):
                    await db.enqueue(f"cli-reconcile:{stamp()}:{row['id']}", "reconcile_client", {"client_id": row['id']})
                print("Reconciliation queued")
            elif command == "replay-test-notes":
                if config.test_mode or not config.crm_notes_enabled:
                    raise ValueError("Set TEST_MODE=false and CRM_NOTES_ENABLED=true")
                async with db.transaction():
                    for row in await db.all("SELECT key FROM crm_notes WHERE state='test'"):
                        await db.execute("UPDATE crm_notes SET state='pending' WHERE key=?", (row['key'],))
                        await db.execute("UPDATE jobs SET state='pending',attempts=0,available_at=? WHERE key=?", (stamp(), 'note:' + row['key']))
                print("Previously simulated notes queued")
            elif command == "prune":
                # Keep idempotency keys and audit events; remove old inbox/contact payloads only.
                cutoff = stamp(now() - timedelta(days=30))
                await db.execute("UPDATE jobs SET payload='{}' WHERE state='done' AND kind IN ('inbox','message') AND created_at<?", (cutoff,))
                await db.execute("DELETE FROM links WHERE expires_at<?", (cutoff,))
                print("Old message payloads and expired links removed")
        finally:
            await crm.http.close()
            await tg.http.close()
            await max_bot.http.close()
            await db.close()


def main():
    parser = argparse.ArgumentParser(description="Sinai bot operations")
    parser.add_argument("command", choices=["serve", "doctor", "setup-webhooks", "plan", "reconcile", "replay-test-notes", "backup", "prune"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "serve":
        uvicorn.run("sinai.app:create_app", factory=True, host="0.0.0.0", port=8000, workers=1, access_log=False)
    elif args.command == "backup":
        config = load_settings()
        target = args.output or Path("backups") / (now().strftime("%Y%m%d-%H%M%S") + ".sqlite3")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.resolve() == config.database_path.resolve():
            raise ValueError("Backup target already exists or is the source")
        with sqlite3.connect(f"file:{config.database_path.resolve().as_posix()}?mode=ro", uri=True) as source:
            with sqlite3.connect(target) as destination:
                source.backup(destination)
        os.chmod(target, 0o600)
        print("Backup created:", target)
    else:
        try:
            asyncio.run(run_command(args.command))
        except Exception as exc:
            # Never echo httpx URLs, Authorization headers or payloads.
            from .http import APIError
            raise SystemExit(str(exc) if isinstance(exc, (APIError, ValueError)) else type(exc).__name__) from None


if __name__ == "__main__":
    main()
