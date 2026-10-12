from .base import Incoming
from ..http import HTTP, APIError, RetryLater, AmbiguousSend
from ..matching import phone


class Telegram:
    def __init__(self, config, http=None):
        self.config = config
        self.http = http or HTTP("telegram", interval=1.05)
        self.poll_http = HTTP('telegram-poll', interval=0.5)
        self.base = "https://api.telegram.org/bot" + config.tg_bot_token.get_secret_value() + "/"

    async def call(self, method, data, safe=False):
        result = await self.http.request("POST", self.base + method, json=data, safe=safe)
        if not result.get("ok"):
            if result.get("error_code") == 429:
                raise RetryLater("telegram", result.get("parameters", {}).get("retry_after", 30))
            raise APIError("telegram", result.get("error_code", 400))
        return result.get("result")

    async def send(self, user_id, chat_id, text, buttons=None, request_contact=False, remove_keyboard=False):
        if self.config.test_mode:
            return "test"
        data = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if request_contact:
            # Telegram ReplyKeyboardMarkup + KeyboardButton(request_contact=True).
            data["reply_markup"] = {"keyboard": [[{"text": "Поделиться моим контактом", "request_contact": True}]],
                                    "resize_keyboard": True, "one_time_keyboard": True}
        elif buttons:
            data["reply_markup"] = {"inline_keyboard": [[{"text": b["text"], **({"url": b["url"]} if "url" in b
                                    else {"callback_data": b["data"]})} for b in row] for row in buttons]}
        elif remove_keyboard:
            data["reply_markup"] = {"remove_keyboard": True}
        result = await self.call("sendMessage", data)
        if not isinstance(result, dict) or not result.get("message_id"):
            raise AmbiguousSend("telegram", 200, "message_id_missing")
        return str(result["message_id"])

    async def answer(self, callback_id):
        if callback_id and not self.config.test_mode:
            await self.call("answerCallbackQuery", {"callback_query_id": callback_id}, safe=True)

    def parse(self, update):
        callback = update.get("callback_query")
        message = callback.get("message", {}) if callback else update.get("message", {})
        if update.get("my_chat_member"):
            item = update["my_chat_member"]
            if item.get("chat", {}).get("type") != "private":
                return None
            if item["new_chat_member"]["status"] in {"kicked", "left"}:
                uid = item["chat"]["id"]
                return Incoming("telegram", uid, uid, stopped=True)
            return None
        if message.get("chat", {}).get("type") != "private":
            return None
        sender = callback["from"] if callback else message.get("from", {})
        if not sender.get("id") or sender.get("is_bot"):
            return None
        return Incoming("telegram", sender["id"], message["chat"]["id"], message.get("text", ""),
                        callback.get("data", "") if callback else "", callback.get("id", "") if callback else "",
                        message.get("contact") if not callback else None, raw=message)

    async def poll(self, cursor):
        data = {'timeout': self.config.poll_timeout, 'limit': 100,
                'allowed_updates': ['message', 'callback_query', 'my_chat_member']}
        if cursor is not None:
            data['offset'] = int(cursor)
        result = await self.poll_http.request('POST', self.base+'getUpdates', json=data,
                                              timeout=self.config.poll_timeout+10)
        if not result.get('ok'):
            raise APIError('telegram-poll', result.get('error_code', 400))
        updates = result.get('result', [])
        return updates, max((x['update_id']+1 for x in updates), default=cursor)

    def verify_contact(self, contact, user_id):
        if contact.get("user_id") != user_id:
            raise ValueError("Отправьте собственный контакт кнопкой под сообщением.")
        return phone(contact["phone_number"])
