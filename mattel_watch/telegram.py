from html import escape
import json
import re
from datetime import datetime, timezone
from .config import launch_time
from .http import TransportError


class DeliveryError(Exception):
    def __init__(self, ambiguous=False, detail="Telegram delivery failed"):
        self.ambiguous = ambiguous
        super().__init__(detail)


def message(product, purchase_state="pending", detail=""):
    variant = product.selected_variant()
    prices = [v.price_cents for v in product.variants if v.price_cents is not None]
    if product.currency and prices:
        low, high = min(prices), max(prices)
        price = f"{product.currency} {low / 100:.2f}"
        if high != low:
            price += f"–{high / 100:.2f}"
    else:
        price = "Not verified — see product page"
    future = product.launch_at and datetime.now(timezone.utc) < launch_time(product.launch_at)
    status = 'Launch/timer verification pending' if product.launch_check=='unknown' else ('Scheduled release — another message follows when it is available' if future else (
        ('Waiting for site purchase control' if product.available and product.ui_buyable is False else 'Available / pre-order') if product.available else "Not available yet / sold out")
        )
    headline = 'Launch watch' if product.launch_at else 'New Hot Wheels'
    body = (f"🚗 <b>{headline} · {escape(' + '.join(product.groups).upper())}</b>\n"
            f"<b>{escape(product.title[:500])}</b>\n"
            f"Price per unit: {escape(price)}\n"
            f"Status: {status}\n"
            f"Delivery: Canada")
    if product.launch_at:
        at = launch_time(product.launch_at)
        if product.launch_label:
            body += '\nSite schedule: ' + escape(product.launch_label)
        body += '\nLaunch (UTC): ' + at.strftime('%d %b %Y %H:%M UTC')
    if detail:
        body += "\n" + escape(detail[:1000])
    body += f'\n<a href="{escape(product.url, quote=True)}">Open product</a>'
    return body


def ready_message(product, detail=""):
    body = f"🟢 <b>Available now — add it to your cart</b>\n<b>{escape(product.title[:500])}</b>"
    if detail:
        body += "\n" + escape(detail[:1000])
    return body + f'\n<a href="{escape(product.url, quote=True)}">Open product</a>'


class Telegram:
    def __init__(self, token, chat_id, http):
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token or ""):
            raise ValueError("Set TELEGRAM_BOT_TOKEN in .env (do not commit it)")
        # A numeric chat/channel ID (channels start with -100) or a public @channelname.
        if not re.fullmatch(r"-?[0-9]+|@[A-Za-z][A-Za-z0-9_]{3,31}", str(chat_id or "")):
            raise ValueError("Set TELEGRAM_CHAT_ID in .env to a numeric chat ID or a public @channelname")
        self.token, self.chat_id, self.http = token, str(chat_id), http

    def _call(self, method, payload):
        try:
            raw = self.http.request(f"https://api.telegram.org/bot{self.token}/{method}", payload)
        except TransportError as exc:
            raise DeliveryError(exc.ambiguous, "Telegram request failed; credentials and URL omitted") from None
        if not isinstance(raw, dict):
            raise DeliveryError(True, "Invalid Telegram response")
        if raw.get("ok") is not True:
            if "message is not modified" in str(raw.get("description", "")).lower():
                return True
            raise DeliveryError(False, "Telegram rejected request; check bot/chat configuration")
        return raw.get("result")

    def send(self, product, purchase_state, detail):
        payload = {"chat_id": self.chat_id, "text": message(product, purchase_state, detail), "parse_mode": "HTML",
                   "reply_markup": {"inline_keyboard": [[{"text": "Open product", "url": product.url}]]}}
        if product.image:
            payload["link_preview_options"] = {"url": product.image, "prefer_large_media": True}
        result = self._call("sendMessage", payload)
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise DeliveryError(True, "Telegram accepted request without a verifiable message ID")
        return result["message_id"]

    def edit(self, message_id, product, purchase_state, detail):
        self._call("editMessageText", {"chat_id": self.chat_id, "message_id": message_id,
                   "text": message(product, purchase_state, detail), "parse_mode": "HTML",
                   "reply_markup": {"inline_keyboard": [[{"text": "Open product", "url": product.url}]]}})

    def notify(self, reply_to, product, detail=""):
        payload = {"chat_id": self.chat_id, "text": ready_message(product, detail), "parse_mode": "HTML",
                   "reply_markup": {"inline_keyboard": [[{"text": "Open product", "url": product.url}]]}}
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        self._call("sendMessage", payload)

    def recent_chats(self):
        """Chats and channels the bot has recently seen, so the channel ID can be copied."""
        updates = self._call("getUpdates", {"allowed_updates": ["message", "channel_post", "my_chat_member"]})
        chats = {}
        for update in updates if isinstance(updates, list) else []:
            for key in ("message", "channel_post", "my_chat_member"):
                chat = (update.get(key) or {}).get("chat") if isinstance(update, dict) else None
                if isinstance(chat, dict) and "id" in chat:
                    chats[chat["id"]] = {"id": chat["id"], "type": chat.get("type"),
                                         "title": chat.get("title") or chat.get("username") or chat.get("first_name")}
        return list(chats.values())

    def test(self):
        self._call("sendMessage", {"chat_id": self.chat_id, "text": "Mattel Watch connected. This is a setup test."})


class ConsoleTelegram:
    """Explicit offline preview; never sends anything to Telegram."""
    def __init__(self):
        self.count = 0

    def send(self, product, purchase_state, detail):
        self.count += 1
        print(json.dumps({"offline_alert": product.to_json(), "status": purchase_state}, ensure_ascii=False))
        return self.count

    def edit(self, message_id, product, purchase_state, detail):
        print(json.dumps({"offline_edit": message_id, "product": product.title, "status": purchase_state, "detail": detail}))

    def notify(self, reply_to, product, detail=""):
        print(json.dumps({"offline_available_now": reply_to, "product": product.title, "detail": detail}))
