"""Environment-backed settings shared by the Avito bot modules."""

import os
from typing import Optional

try:
    from dotenv import load_dotenv  # type: ignore
except ImportError:
    def load_dotenv(*args, **kwargs):  # type: ignore
        return None


load_dotenv()

POLL_PERIOD_SEC = int(os.getenv("POLL_PERIOD_SEC", "180"))
POLL_PERIOD_MAX_SEC = int(os.getenv("POLL_PERIOD_MAX_SEC", "300"))
AVITO_REQUEST_GAP_SEC = float(os.getenv("AVITO_REQUEST_GAP_SEC", "5"))
AVITO_PROXIES = [value.strip() for value in os.getenv("AVITO_PROXIES", "").split(",") if value.strip()]
AVITO_PROXY_CHANGE_URLS = [
    value.strip()
    for value in os.getenv("AVITO_PROXY_CHANGE_URLS", "").split(",")
    if value.strip()
]
AVITO_ENRICH = os.getenv("AVITO_ENRICH", "0") == "1"
API_URLS_FILE = os.getenv("API_URLS_FILE", "api_urls.json")
SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/Multiscan_service1")
PRIME_ON_START = os.getenv("PRIME_ON_START", "1") == "1"
START_STRICT = os.getenv("START_STRICT", "1") == "1"
START_GRACE_SEC = int(os.getenv("START_GRACE_SEC", "10"))
DISPLAY_TZ_NAME = os.getenv("DISPLAY_TZ", "Europe/Moscow")
DISPLAY_TZ_OFFSET_MIN = int(os.getenv("DISPLAY_TZ_OFFSET_MIN", "180"))

_admin_chat_id = os.getenv("ADMIN_CHAT_ID")
ADMIN_CHAT_ID: Optional[int] = (
    int(_admin_chat_id)
    if _admin_chat_id and _admin_chat_id.lstrip("-").isdigit()
    else None
)

ALERT_BOT_TOKEN = os.getenv("ALERT_BOT_TOKEN")
ALERT_BOT_USERNAME = os.getenv("ALERT_BOT_USERNAME", "")
# Optional Telegram message effect ID. Effects are supported in private chats;
# an alert is retried without the effect if Telegram rejects the configured ID.
ALERT_MESSAGE_EFFECT_ID = os.getenv("ALERT_MESSAGE_EFFECT_ID") or None
TELEGRAM_PRIMARY_BUTTON_ICON_ID = os.getenv("TELEGRAM_PRIMARY_BUTTON_ICON_ID") or None
TELEGRAM_SUCCESS_BUTTON_ICON_ID = os.getenv("TELEGRAM_SUCCESS_BUTTON_ICON_ID") or None
TELEGRAM_DANGER_BUTTON_ICON_ID = os.getenv("TELEGRAM_DANGER_BUTTON_ICON_ID") or None
BINDINGS_FILE = os.getenv("BINDINGS_FILE", "user_bindings.json")
ALERT_LINKS_FILE = os.getenv("ALERT_LINKS_FILE", "alert_links.json")

KEYS_FILE = os.getenv("KEYS_FILE", "issued_keys.json")
SENT_FILE = os.getenv("SENT_FILE", "sent_ads.json")
ACCOUNTS_FILE = os.getenv("ACCOUNTS_FILE", "accounts.json")
DEDUP_TTL_DAYS = int(os.getenv("DEDUP_TTL_DAYS", "14"))
SUBSCRIPTIONS_FILE = os.getenv("SUBSCRIPTIONS_FILE", "subscriptions.json")
