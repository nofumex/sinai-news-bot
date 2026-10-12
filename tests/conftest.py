import copy
import json
from types import SimpleNamespace
import pytest
from sinai.admin import Admin
from sinai.config import Settings
from sinai.db import Database, stamp
from sinai.matching import contact_phones, name_score
from sinai.platforms import Telegram, Max
from sinai.scheduler import Scheduler
from sinai.service import Service
from sinai.worker import Worker


def crm_contact(cid=101, name="Иванов Иван Иванович", number="8 (999) 123-45-67", lead_ids=(201,)):
    return {"id": cid, "name": name, "custom_fields_values": [{"field_code": "PHONE", "values": [{"value": number}]}],
            "_embedded": {"leads": [{"id": x} for x in lead_ids]}}


class FakeCRM:
    def __init__(self):
        self.contacts = {101: crm_contact()}
        self.leads = {201: {"id": 201, "pipeline_id": 1312204, "status_id": 123,
                            "_embedded": {"contacts": [{"id": 101}]}}}
        self.notes = []
        self.lead_reads = 0

    async def contact(self, cid):
        return copy.deepcopy(self.contacts[cid])

    async def lead(self, lid):
        self.lead_reads += 1
        return copy.deepcopy(self.leads[lid])

    async def leads_for(self, contact):
        return [await self.lead(x["id"]) for x in contact["_embedded"]["leads"]]

    async def by_phone(self, number):
        return [copy.deepcopy(x) for x in self.contacts.values() if number in contact_phones(x)]

    async def by_name(self, name):
        return [dict(copy.deepcopy(x), match_score=name_score(name, x['name'])) for x in self.contacts.values()
                if name_score(name, x['name']) >= .60]

    async def write_note(self, key):
        self.notes.append(key)

    async def verify_pipeline(self):
        return {"id": 8993570, "name": "Успешно завершенные"}


@pytest.fixture
async def system(tmp_path):
    config = Settings(_env_file=None, database_path=tmp_path / "test.sqlite3", test_mode=True,
                      tg_bot_token="fake-token", max_bot_token="fake-max-token", max_bot_username="test_max_bot")
    db = Database(config.database_path)
    await db.open()
    crm = FakeCRM()
    platforms = {"telegram": Telegram(config), "max": Max(config)}
    service = Service(config, db, crm, platforms)
    scheduler = Scheduler(config, db, service)
    service.admin = Admin(config, db, service, scheduler)
    worker = Worker(config, db, service, scheduler)
    yield SimpleNamespace(config=config, db=db, crm=crm, platforms=platforms, service=service, scheduler=scheduler, worker=worker)
    for platform in platforms.values():
        await platform.http.close()
    await db.close()


async def subscriber(system, platform="telegram", uid=50, segment="active", number="+79991234567", at=None):
    cursor = await system.db.execute("INSERT INTO subscriptions(platform,user_id,chat_id,phone,selected_segment,state,subscribed_at,created_at) VALUES(?,?,?,?,?,'syncing',?,?)",
                                      (platform, uid, uid, number, segment, stamp(at), stamp()))
    return cursor.lastrowid


async def registered(system, **kwargs):
    sid = await subscriber(system, **kwargs)
    system.service.context = "registration-" + str(sid)
    system.service.sequence = 0
    await system.service.sync(sid)
    return await system.db.one("SELECT * FROM subscriptions WHERE id=?", (sid,))


def tg_update(text=None, callback=None, contact=None, uid=50, update_id=1):
    message = {"message_id": 1, "chat": {"id": uid, "type": "private"}, "from": {"id": uid}, "text": text or ""}
    if contact:
        message["contact"] = contact
    if callback:
        return {"update_id": update_id, "callback_query": {"id": str(update_id), "from": {"id": uid}, "message": message, "data": callback}}
    return {"update_id": update_id, "message": message}


async def messages(system):
    rows = await system.db.all("SELECT payload FROM jobs WHERE kind='message' ORDER BY id")
    return [json.loads(row['payload']) for row in rows]
