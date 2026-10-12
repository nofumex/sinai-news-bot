import hashlib
import hmac
import ssl
import vobject
from .base import Incoming
from ..http import HTTP, APIError, AmbiguousSend, RetryLater
from ..matching import phone


class Max:
    def __init__(self, config, http=None):
        self.config = config
        context = ssl.create_default_context()
        if config.max_ca_bundle:
            context.load_verify_locations(config.max_ca_bundle)
        self.http = http or HTTP("max", interval=0.55, verify=context)
        self.poll_http = HTTP('max-poll', interval=config.max_poll_min_seconds, verify=context)
        self.base = "https://platform-api2.max.ru"

    async def call(self, method, path, *, params=None, json=None, safe=False):
        data = await self.http.request(method, self.base + path, params=params, json=json, safe=safe,
                                       headers={"Authorization": self.config.max_bot_token.get_secret_value()})
        if data.get('code') == 'attachment.not.ready':
            raise RetryLater('max',15)
        if data.get("success") is False:
            raise APIError("max", 400, "unsuccessful_result")
        return data

    async def send(self, user_id, chat_id, text, buttons=None, request_contact=False, remove_keyboard=False):
        if self.config.test_mode:
            return "test"
        data = {"text": text}
        if request_contact:
            rows = [[{"type": "request_contact", "text": "Поделиться моим контактом"}]]
        else:
            rows = [[{"text": b["text"], **({"type": "link", "url": b["url"]} if "url" in b
                     else {"type": "callback", "payload": b["data"]})} for b in row] for row in (buttons or [])]
        if rows:
            data["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": rows}}]
        result = await self.call("POST", "/messages", params={"user_id": user_id, "disable_link_preview": True}, json=data)
        mid = ((result.get("message") or {}).get("body") or {}).get("mid")
        if not mid:
            raise AmbiguousSend("max", 200, "message_id_missing")
        return mid

    async def answer(self, callback_id):
        if callback_id and not self.config.test_mode:
            await self.call("POST", "/answers", params={"callback_id": callback_id}, json={}, safe=True)

    def parse(self, update):
        kind = update.get("update_type")
        if kind in {"bot_started", "bot_stopped", "dialog_removed"}:
            user = update.get("user", {})
            if not user.get("user_id"):
                return None
            return Incoming("max", user["user_id"], update.get("chat_id", user["user_id"]),
                            text="/start " + (update.get("payload") or "") if kind == "bot_started" else "",
                            stopped=kind != "bot_started")
        if kind not in {"message_created", "message_callback"}:
            return None
        message = update.get("message") or {}
        if message.get("recipient", {}).get("chat_type") != "dialog":
            return None
        callback = update.get("callback", {})
        user = (callback.get("user") if callback else message.get("sender")) or {}
        if not user.get("user_id") or user.get("is_bot"):
            return None
        body = message.get("body") or {}
        contact = next((x["payload"] for x in body.get("attachments") or []
                        if x.get("type") == "contact"), None) if not callback else None
        return Incoming("max", user["user_id"], message.get("recipient", {}).get("chat_id", user["user_id"]),
                        body.get("text") or "", callback.get("payload", ""),
                        callback.get("callback_id", ""), contact, raw=message)

    async def poll(self, cursor):
        params = {'timeout': self.config.poll_timeout, 'limit': 100,
                  'types': 'bot_started,bot_stopped,message_created,message_callback'}
        if cursor is not None:
            params['marker'] = int(cursor)
        data = await self.poll_http.request('GET', self.base+'/updates', params=params,
                                            headers={'Authorization': self.config.max_bot_token.get_secret_value()},
                                            timeout=self.config.poll_timeout+10)
        return data.get('updates', []), data.get('marker')

    def verify_contact(self, payload, user_id):
        raw = payload.get("vcf_info", "")
        # JSON decoding already converts CRLF escapes; some payloads have literal escapes.
        raw = raw.replace("\\r\\n", "\r\n")
        digest = hmac.new(self.config.max_bot_token.get_secret_value().encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not payload.get("hash") or not hmac.compare_digest(digest, str(payload["hash"]).lower()):
            raise ValueError("Контакт не подтверждён MAX. Нажмите кнопку «Поделиться моим контактом».")
        info = payload.get("max_info") or {}
        if info.get("user_id") is not None and int(info["user_id"]) != user_id:
            raise ValueError("Контакт принадлежит другому пользователю.")
        card = vobject.readOne(raw)
        values = {phone(x.value) for x in card.contents.get("tel", [])}
        if len(values) != 1:
            raise ValueError("Контакт должен содержать один подтверждённый номер.")
        return values.pop()
