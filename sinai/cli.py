import argparse
import asyncio
import json
import os
import sqlite3
from datetime import timedelta
from pathlib import Path
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
            elif command == "setup-polling":
                if config.tg_bot_token.get_secret_value():
                    await tg.call('deleteWebhook',{'drop_pending_updates':False},safe=True)
                    print('Telegram polling enabled; pending updates preserved')
                if config.max_bot_token.get_secret_value():
                    data = await max_bot.call('GET','/subscriptions',safe=True)
                    for item in data.get('subscriptions',[]):
                        await max_bot.call('DELETE','/subscriptions',params={'url':item['url']},safe=True)
                    print('MAX webhook subscriptions removed for this bot')
                print('amoCRM uses periodic reads; existing CRM webhooks are not modified')
            elif command == "plan":
                service = Service(config, db, crm, {"telegram": tg, "max": max_bot})
                print(json.dumps(await Scheduler(config, db, service).plan(), ensure_ascii=True, indent=2))
            elif command == "reconcile":
                for row in await db.all("SELECT id FROM clients WHERE merged_into IS NULL"):
                    await db.enqueue(f"cli-reconcile:{stamp()}:{row['id']}", "reconcile_client", {"client_id": row['id']})
                print("Reconciliation queued")
                for row in await db.all('SELECT id FROM subscriptions WHERE phone IS NOT NULL AND identity_verified=0'):
                    await db.enqueue(f"cli-sync:{stamp()}:{row['id']}",'sync',{'subscription_id':row['id']})
            elif command == 'migrate':
                print('Database migrations applied and foreign keys verified')
            elif command == 'status':
                print(json.dumps({'cursors':await db.all("SELECT key,value FROM settings WHERE key LIKE 'poll_cursor:%' OR key LIKE 'last_poll:%'"),
                                  'jobs':await db.all('SELECT state,COUNT(*) n FROM jobs GROUP BY state')},ensure_ascii=True))
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
            await tg.poll_http.close()
            await max_bot.http.close()
            await max_bot.poll_http.close()
            await db.close()


def main():
    parser = argparse.ArgumentParser(description="Sinai bot operations")
    parser.add_argument("command", choices=["serve", "doctor", "setup-polling", "migrate", "status", "health", "plan", "reconcile", "replay-test-notes", "backup", "prune"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "serve":
        from .app import serve
        try:
            asyncio.run(serve())
        except KeyboardInterrupt:
            pass
    elif args.command in {'status','health'}:
        config = load_settings()
        with sqlite3.connect(f"file:{config.database_path.resolve().as_posix()}?mode=ro",uri=True) as db:
            state = dict(db.execute("SELECT key,value FROM settings WHERE key LIKE 'poll_cursor:%' OR key LIKE 'last_poll:%' OR key='worker_heartbeat'"))
            if args.command == 'health':
                from datetime import datetime
                if not state.get('worker_heartbeat') or (now()-datetime.fromisoformat(state['worker_heartbeat'])).total_seconds() > 180:
                    raise SystemExit(1)
            else:
                print(json.dumps({'runtime':state,'jobs':dict(db.execute('SELECT state,COUNT(*) FROM jobs GROUP BY state'))},ensure_ascii=True))
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
