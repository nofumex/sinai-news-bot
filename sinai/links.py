import hashlib
import re
import secrets
from datetime import timedelta
from urllib.parse import urlparse
from .db import now, stamp


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def parse_lead(value, base_url):
    if value.isdigit() and int(value) > 0:
        return int(value)
    url = urlparse(value)
    if url.scheme != "https" or url.hostname != urlparse(base_url).hostname:
        raise ValueError("Укажите ID или HTTPS-ссылку сделки в sinai24.amocrm.ru.")
    match = re.fullmatch(r"/leads/detail/(\d+)/?", url.path)
    if not match:
        raise ValueError("Ожидается ссылка /leads/detail/ID.")
    return int(match[1])


async def issue(db, config, platform, lead_id, contact_id, actor):
    token = secrets.token_urlsafe(32)
    await db.execute("INSERT INTO links(token_hash,platform,lead_id,contact_id,expires_at,issued_by,created_at) VALUES(?,?,?,?,?,?,?)",
                     (token_hash(token), platform, lead_id, contact_id, stamp(now() + timedelta(seconds=config.link_ttl_seconds)),
                      actor, stamp()))
    username = config.tg_bot_username if platform == "telegram" else config.max_bot_username
    base = "https://t.me/" if platform == "telegram" else "https://max.ru/"
    return base + username + "?start=" + token
