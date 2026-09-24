import json
from dataclasses import dataclass
from typing import List, Optional
from urllib import error, parse, request


class TelegramError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramUpdate:
    update_id: int
    user_id: int
    chat_id: int
    text: str


class TelegramClient:
    def __init__(self, token: str, api_base: str = "https://api.telegram.org"):
        self._token = token
        self._api_base = api_base.rstrip("/")

    def _call(self, method: str, fields: dict, *, timeout: int):
        url = f"{self._api_base}/bot{self._token}/{method}"
        data = parse.urlencode(fields).encode("utf-8")
        req = request.Request(url, data=data, method="POST")
        try:
            with request.urlopen(req, timeout=timeout) as response:
                raw = response.read()
        except (error.HTTPError, error.URLError, OSError) as exc:
            raise TelegramError("Telegram API request failed") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelegramError("Telegram API returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise TelegramError("Telegram API returned an unsuccessful response")
        return payload.get("result")

    def get_updates(self, offset: Optional[int], timeout: int = 25) -> List[TelegramUpdate]:
        fields = {"timeout": str(timeout)}
        if offset is not None:
            fields["offset"] = str(offset)
        result = self._call("getUpdates", fields, timeout=max(timeout + 5, 10))
        updates: List[TelegramUpdate] = []
        if not isinstance(result, list):
            return updates
        for item in result:
            if not isinstance(item, dict):
                continue
            message = item.get("message")
            if not isinstance(message, dict):
                continue
            sender = message.get("from")
            chat = message.get("chat")
            text = message.get("text")
            if not isinstance(sender, dict) or not isinstance(chat, dict) or not isinstance(text, str):
                continue
            try:
                updates.append(
                    TelegramUpdate(
                        update_id=int(item["update_id"]),
                        user_id=int(sender["id"]),
                        chat_id=int(chat["id"]),
                        text=text,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return updates

    def send_message(self, chat_id: int, text: str) -> None:
        self._call("sendMessage", {"chat_id": str(chat_id), "text": text}, timeout=15)
