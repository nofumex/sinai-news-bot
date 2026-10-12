import asyncio
import hashlib
import logging
import random
import time
from .db import dumps, stamp
from .http import APIError, RetryLater

log = logging.getLogger('sinai.polling')


def event_key(platform, update):
    if platform == 'telegram' and 'update_id' in update:
        return f"telegram:{update['update_id']}"
    if platform == 'max':
        callback = (update.get('callback') or {}).get('callback_id')
        mid = ((update.get('message') or {}).get('body') or {}).get('mid')
        if callback:
            return 'max:callback:'+str(callback)
        if mid:
            return f"max:{update.get('update_type')}:{mid}"
    return platform+':'+hashlib.sha256(dumps(update).encode()).hexdigest()


class Poller:
    def __init__(self, config, db, service, platform):
        self.config, self.db, self.service, self.platform = config, db, service, platform
        self.stopping = asyncio.Event()
        self.heartbeat = time.monotonic()

    async def ingest(self, updates, cursor):
        async with self.db.transaction():
            for update in updates:
                await self.db.enqueue(event_key(self.platform, update), 'inbox', {'platform': self.platform, 'update': update})
                incoming = self.service.platforms[self.platform].parse(update)
                if incoming and (incoming.stopped or incoming.text.strip() == '/stop'):
                    sub = await self.db.one('SELECT * FROM subscriptions WHERE platform=? AND user_id=?', (self.platform, incoming.user_id))
                    if sub:
                        await self.service.opt_out(sub, in_transaction=True)
            if cursor is not None:
                await self.db.set_setting('poll_cursor:'+self.platform, cursor)
            await self.db.set_setting('last_poll:'+self.platform, stamp())

    async def once(self):
        cursor = await self.db.setting('poll_cursor:'+self.platform, None)
        updates, next_cursor = await self.service.platforms[self.platform].poll(cursor)
        await self.ingest(updates, next_cursor)

    async def run(self):
        failures = 0
        while not self.stopping.is_set():
            start = time.monotonic()
            self.heartbeat = start
            try:
                await self.once()
                failures = 0
                delay = max(0.5, (self.config.max_poll_min_seconds if self.platform == 'max' else 0.5)-(time.monotonic()-start))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                delay = exc.seconds if isinstance(exc, RetryLater) else min(60, 2**min(failures, 6)+random.random())
                message = str(exc) if isinstance(exc, APIError) else type(exc).__name__
                log.warning('%s polling failure: %s', self.platform, message)
                await self.db.execute('INSERT INTO errors(kind,message,created_at) VALUES(?,?,?)', (self.platform+'_poll', message, stamp()))
                if isinstance(exc,APIError) and exc.status == 409:
                    raise  # A conflicting deployment must stop this worker, not keep broadcasting.
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=delay)
            except TimeoutError:
                pass
