from dataclasses import dataclass


@dataclass
class Incoming:
    platform: str
    user_id: int
    chat_id: int
    text: str = ""
    callback: str = ""
    callback_id: str = ""
    contact: dict | None = None
    stopped: bool = False
    raw: dict | None = None


def button(text, data):
    return {"text": text, "data": data}


def link_button(text, url):
    return {"text": text, "url": url}
