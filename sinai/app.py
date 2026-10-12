"""Polling runtime: no inbound HTTP listener or public domain."""
import asyncio
import logging
from contextlib import asynccontextmanager
from filelock import FileLock
from .admin import Admin
from .campaigns import Campaigns
from .config import load_settings
from .crm import CRM
from .db import Database
from .http import APIError
from .platforms import Telegram,Max
from .polling import Poller
from .scheduler import Scheduler
from .service import Service
from .worker import Worker


def configure_logs(level):
    logging.basicConfig(level=level,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    for name in ('httpx','httpcore'):
        logging.getLogger(name).setLevel(logging.CRITICAL)


class Runtime:
    def __init__(self,config=None):
        self.config = config or load_settings()
        configure_logs(self.config.log_level)
        self.db = Database(self.config.database_path)
        self.crm = CRM(self.config,self.db)
        self.platforms = {'telegram':Telegram(self.config),'max':Max(self.config)}
        self.service = Service(self.config,self.db,self.crm,self.platforms)
        self.scheduler = Scheduler(self.config,self.db,self.service)
        self.service.admin = Admin(self.config,self.db,self.service,self.scheduler)
        self.service.campaigns = Campaigns(self.config,self.db,self.service,self.scheduler)
        self.worker = Worker(self.config,self.db,self.service,self.scheduler)
        self.tasks = []
        self.pollers = []

    @asynccontextmanager
    async def lifespan(self):
        self.config.database_path.parent.mkdir(parents=True,exist_ok=True)
        lock = FileLock(str(self.config.database_path)+'.process.lock')
        try:
            lock.acquire(timeout=0)
            await self.db.open()
            if self.config.polling_enabled:
                # Refuse active webhooks instead of silently reconfiguring another deployment.
                if self.config.tg_bot_token.get_secret_value():
                    result = await self.platforms['telegram'].call('getWebhookInfo',{},safe=True)
                    if result.get('url'):
                        raise APIError('telegram',409,'webhook_active_run_setup_polling')
                    self.pollers.append(Poller(self.config,self.db,self.service,'telegram'))
                if self.config.max_bot_token.get_secret_value():
                    result = await self.platforms['max'].call('GET','/subscriptions',safe=True)
                    if result.get('subscriptions'):
                        raise APIError('max',409,'webhook_active_run_setup_polling')
                    self.pollers.append(Poller(self.config,self.db,self.service,'max'))
            self.tasks = [asyncio.create_task(self.worker.run(),name='sinai-worker')]
            self.tasks += [asyncio.create_task(p.run(),name='sinai-poll-'+p.platform) for p in self.pollers]
            yield self
        finally:
            self.worker.stopping.set()
            for poller in self.pollers:
                poller.stopping.set()
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks,return_exceptions=True)
            await self.crm.http.close()
            for platform in self.platforms.values():
                await platform.http.close()
                await platform.poll_http.close()
            await self.db.close()
            lock.release()

    async def run(self):
        async with self.lifespan():
            # A failed processing task brings down the service; systemd/Compose restarts it.
            await asyncio.gather(*self.tasks)


async def serve():
    await Runtime().run()
