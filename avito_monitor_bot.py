# -*- coding: utf-8 -*-
"""
Avito Monitor Bot (aiogram 3.x)
— v5.0 (API-first) — ИСПРАВЛЕННАЯ ВЕРСИЯ
    • Переход на JSON API Авито (web/1/js/items) вместо мёртвого HTML-парсера
    • Классификация блокировок Qrator (403 rate_limit, 429 ip_block, 439 challenge)
    • Прогрев сессии + браузерные заголовки
    • Умный бэкофф с Retry-After + экспонентой
    • Удобное управление (инлайн-меню + статусы)
    • Легко добавлять прокси со сменой IP
    • Rerun `python test_regressions.py` — все тесты должны пройти
"""

import os
import re
import time
import asyncio
import html
import base64
import json
import aiohttp
import random
from avito_api import (
    AvitoBlock,
    AvitoHttpClient,
    AvitoHttpError,
    convert_url_to_api,
    invalidate_cached_api_url,
    parse_api_items,
)
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Deque, Callable, cast, Any, Set
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse, parse_qs, parse_qsl, urlencode, unquote
import uuid
import logging
import secrets

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from bs4 import BeautifulSoup  # type: ignore[reportMissingImports]
from bs4.element import Tag
from storage import load_json, save_json, update_json

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo  # py>=3.9
except Exception:
    ZoneInfo = None  # type: ignore

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    def load_dotenv(*args, **kwargs):  # type: ignore
        return None

load_dotenv()

# keep-alive (Replit/Render). Если модуля нет — no-op.
try:
    from background import keep_alive  # type: ignore
except Exception:
    def keep_alive():  # type: ignore
        return None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# ===== ENV =====
POLL_PERIOD_SEC = int(os.getenv("POLL_PERIOD_SEC", "180"))    # минимальный период опроса (сек)
POLL_PERIOD_MAX_SEC = int(os.getenv("POLL_PERIOD_MAX_SEC", "300"))
AVITO_REQUEST_GAP_SEC = float(os.getenv("AVITO_REQUEST_GAP_SEC", "5"))
AVITO_PROXIES = [value.strip() for value in os.getenv("AVITO_PROXIES", "").split(",") if value.strip()]
AVITO_PROXY_CHANGE_URLS = [value.strip() for value in os.getenv("AVITO_PROXY_CHANGE_URLS", "").split(",") if value.strip()]
AVITO_ENRICH = os.getenv("AVITO_ENRICH", "0") == "1"          # догрузка страниц объявлений (доп. запросы = выше риск блока)
API_URLS_FILE = os.getenv("API_URLS_FILE", "api_urls.json")  # кэш конвертации URL -> API
SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/Multiscan_service1")
PRIME_ON_START = os.getenv("PRIME_ON_START", "1") == "1"
START_STRICT = os.getenv("START_STRICT", "0") == "1"
START_GRACE_SEC = int(os.getenv("START_GRACE_SEC", "10"))
DISPLAY_TZ_NAME = os.getenv("DISPLAY_TZ", "Europe/Moscow")
DISPLAY_TZ_OFFSET_MIN = int(os.getenv("DISPLAY_TZ_OFFSET_MIN", "180"))

# ADMIN_CHAT_ID как int (или None)
_ADMIN_STR = os.getenv("ADMIN_CHAT_ID")
ADMIN_CHAT_ID: Optional[int] = int(_ADMIN_STR) if (_ADMIN_STR and _ADMIN_STR.lstrip("-").isdigit()) else None

# ===== Настройки alert-бота =====
ALERT_BOT_TOKEN   = os.getenv("ALERT_BOT_TOKEN")              # токен бота-оповещателя
ALERT_BOT_USERNAME = os.getenv("ALERT_BOT_USERNAME", "")      # юзернейм бота-оповещателя без @
BINDINGS_FILE     = os.getenv("BINDINGS_FILE", "user_bindings.json")  # привязки main_user_id -> alert_chat_id
ALERT_LINKS_FILE  = os.getenv("ALERT_LINKS_FILE", "alert_links.json")

# ===== Файлы хранения =====
KEYS_FILE = os.getenv("KEYS_FILE", "issued_keys.json")
SENT_FILE = os.getenv("SENT_FILE", "sent_ads.json")           # { user_id: { ad_id: ts }, "_global": {...} }
ACCOUNTS_FILE = os.getenv("ACCOUNTS_FILE", "accounts.json")   # учётка пользователя
DEDUP_TTL_DAYS = int(os.getenv("DEDUP_TTL_DAYS", "14"))       # сколько хранить следы
DEDUP_GLOBAL = os.getenv("DEDUP_GLOBAL", "1") == "1"          # глобальный антидубликат
SUBSCRIPTIONS_FILE = os.getenv("SUBSCRIPTIONS_FILE", "subscriptions.json")

# ===== Регэксп ключа =====
KEY_RE = re.compile(
    r'(?i)(?:^|\s)(?:ключ\s*:\s*)?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:\s|$)'
)

# ===== helpers =====
def _tz():
    if ZoneInfo:
        try:
            return ZoneInfo(DISPLAY_TZ_NAME)
        except Exception:
            pass
    # fallback — фиксированный сдвиг
    return timezone(timedelta(minutes=DISPLAY_TZ_OFFSET_MIN))

def _fmt_dt(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(float(ts), _tz()).strftime("%d.%m.%Y %H:%M:%S")

def _parse_price_input(value: str) -> Optional[int]:
    normalized = (value or "").strip().replace("\u00a0", "").replace(" ", "").replace(".", "")
    return int(normalized) if normalized.isdigit() else None

def _br(text: str) -> str:
    """
    Больше НЕ используем <br>, чтобы не ловить ошибку Telegram:
    'can't parse entities: Unsupported start tag "br"'.
    На входе может прийти текст, где уже есть <br> — мы превратим их в \\n.
    """
    t = (text or "")
    # обезвреживаем все варианты <br>
    t = t.replace("<br/>", "\n").replace("<br />", "\n").replace("<br>", "\n")
    t = t.replace("\\n", "\n")
    return t

# ==== LICENSE ====
class LicenseManager:
    def __init__(self):
        self._expires: Dict[int, float] = {}  # user_id -> epoch_seconds (UTC)

    def activate_for(self, user_id: int, hours: int = 24):
        self._expires[user_id] = (time.time() + hours * 3600)

    def activate_until(self, user_id: int, expires_ts: float):
        self._expires[user_id] = float(expires_ts)

    def is_active(self, user_id: int) -> bool:
        ts = self._expires.get(user_id)
        return (ts is not None) and (ts > time.time())

    def expiry_dt(self, user_id: int) -> Optional[datetime]:
        ts = self._expires.get(user_id)
        return datetime.fromtimestamp(ts, _tz()) if ts else None


@dataclass
class SubscriberFilter:
    keywords_all: List[str] = field(default_factory=list)  # целевые
    keywords_any: List[str] = field(default_factory=list)
    keywords_stop: List[str] = field(default_factory=list)  # СТОП-слова
    price_min: Optional[int] = None
    price_max: Optional[int] = None


@dataclass
class Subscription:
    id: int
    user_id: int
    search_key: str
    url: str
    flt: SubscriberFilter = field(default_factory=SubscriberFilter)
    name: Optional[str] = None            # название
    only_new: bool = True                 # «только новые»
    forward_chat_id: Optional[int] = None # не используется
    started_ts: float = field(default_factory=lambda: time.time())  # точка отсечения «до создания слота»


def _normalize_url(url: str) -> str:
    u = urlparse(url.strip())
    if not u.scheme:
        u = u._replace(scheme="https")
    netloc = u.netloc or "www.avito.ru"
    keep: Dict[str, str] = {}
    for k, v in parse_qsl(u.query, keep_blank_values=True):
        if k.lower().startswith("utm_"):
            continue
        keep[k] = v
    query = urlencode(sorted(keep.items()))
    u2 = u._replace(netloc=netloc, query=query)
    return urlunparse(u2)


def search_key_from_url(url: str) -> str:
    return _normalize_url(url)


def is_valid_avito_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    return parsed.scheme in {"http", "https"} and (host == "avito.ru" or host.endswith(".avito.ru"))


def avito_short_url(full_url: str) -> str:
    m = re.search(r"(?:/|_)(\d{7,})(?:[/?#]|$)", full_url)
    if m:
        return f"https://www.avito.ru/{m.group(1)}"
    u = urlparse(full_url)
    return urlunparse((u.scheme or "https", u.netloc or "www.avito.ru", u.path, "", "", ""))


def _extract_ad_id(url: str) -> str:
    match = re.search(r"(?:/|_)(\d{7,})(?:[/?#]|$)", url)
    return match.group(1) if match else url


@dataclass
class Ad:
    ad_id: str
    url: str
    title: str = ""
    price: Optional[int] = None
    location: str = ""
    date_str: str = ""
    published_ts: Optional[float] = None
    description: str = ""
    image_url: Optional[str] = None
    seller_name: Optional[str] = None
    seller_id: Optional[str] = None
    price_badge: Optional[str] = None
    features: List[str] = field(default_factory=list)  # например: "Рассрочка"
    views: Optional[int] = None
    is_verified: bool = False


def _attr_to_str(v: object) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)) and v and isinstance(v[0], str):
        return v[0]
    return ""


def _get_text(tag: Optional[Tag]) -> str:
    """Безопасно извлекает текст из bs4-объекта"""
    if tag is None:
        return ""
    try:
        return tag.get_text(strip=True)
    except Exception:
        return str(tag) if tag else ""


def try_extract_filters_from_url(url: str) -> SubscriberFilter:
    flt = SubscriberFilter()
    try:
        u = urlparse(url)
        qs = parse_qs(u.query)
        if "q" in qs and qs["q"]:
            qval = unquote(qs["q"][0]).strip()
            words = [w for w in re.split(r"[,\s]+", qval) if w]
            if words:
                flt.keywords_all = words

        fval = qs.get("f", [None])[0]
        if fval:
            s = fval.strip()
            pad = '=' * ((4 - len(s) % 4) % 4)
            raw = base64.urlsafe_b64decode(s + pad)
            try:
                j = json.loads(raw.decode("utf-8", errors="ignore"))
                text_parts: List[str] = []
                if isinstance(j, dict):
                    def pick(obj, keys):
                        for k in keys:
                            v = obj.get(k)
                            if isinstance(v, str) and v:
                                text_parts.append(v)
                    pick(j, ["brand", "model", "storage", "keyword", "q"])
                    if "params" in j and isinstance(j["params"], list):
                        for p in j["params"]:
                            if isinstance(p, dict):
                                pick(p, ["brand", "model", "storage", "value", "name"])
                if text_parts:
                    for token in text_parts:
                        for w in re.split(r"[,\s/]+", str(token)):
                            w = w.strip()
                            if w and w.lower() not in [x.lower() for x in flt.keywords_all]:
                                flt.keywords_all.append(w)
            except Exception:
                pass
    except Exception:
        pass
    return flt


class Watcher:
    _request_lock = asyncio.Lock()
    _last_request_at = 0.0
    _route_blocked_until: Dict[str, float] = {}

    def __init__(
        self,
        search_key: str,
        url: str,
        bot: Bot,
        on_deliver: Optional[Callable[[int, Ad], None]] = None,
    ):
        self.search_key = search_key
        self.url = url
        self.bot = bot
        self.subscribers: Dict[int, Subscription] = {}
        self.task: Optional[asyncio.Task] = None
        self.seen: Dict[str, float] = {}
        self.interval_min = max(30.0, float(POLL_PERIOD_SEC))
        self.interval_max = max(self.interval_min, float(POLL_PERIOD_MAX_SEC))
        self._interval = self.interval_min
        self._proxy_index = abs(hash(search_key)) % len(AVITO_PROXIES) if AVITO_PROXIES else 0
        self._client = AvitoHttpClient(proxy=self._proxy(), proxy_change_url=self._proxy_change_url())
        self.on_deliver = on_deliver
        self._enrich_cache: set[str] = set()
        self._api_url: Optional[str] = None
        self._blocked_until = 0.0
        self._consecutive_blocks = 0
        self.last_http_status: Optional[int] = None
        self.last_block_kind: Optional[str] = None

    async def start(self):
        if self.task and not self.task.done():
            return
        if PRIME_ON_START:
            await self._prime_seen(30)
        self.task = asyncio.create_task(self._run(), name=f"watch:{self.search_key}")

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        self._client.close()

    def has_subscribers(self):
        return bool(self.subscribers)

    def add_sub(self, sub: Subscription):
        self.subscribers[sub.id] = sub

    def remove_sub(self, sub_id: int):
        self.subscribers.pop(sub_id, None)

    def _proxy(self) -> Optional[str]:
        return AVITO_PROXIES[self._proxy_index] if AVITO_PROXIES else None

    def _route_key(self) -> str:
        return self._proxy() or "direct"

    def _proxy_change_url(self) -> Optional[str]:
        if AVITO_PROXY_CHANGE_URLS and self._proxy_index < len(AVITO_PROXY_CHANGE_URLS):
            return AVITO_PROXY_CHANGE_URLS[self._proxy_index] or None
        return None

    def _rotate_proxy(self) -> bool:
        changed_ip = self._client.request_new_ip()
        if len(AVITO_PROXIES) > 1:
            self._proxy_index = (self._proxy_index + 1) % len(AVITO_PROXIES)
            self._client.set_proxy(self._proxy(), self._proxy_change_url())
            return True
        self._client.reset()
        return changed_ip

    async def _wait_global_rate_limit(self) -> None:
        async with Watcher._request_lock:
            wait = AVITO_REQUEST_GAP_SEC - (time.monotonic() - Watcher._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            Watcher._last_request_at = time.monotonic()

    async def _avito_get(self, url: str):
        await self._wait_global_rate_limit()
        if not self._client.warmed_at:
            await asyncio.to_thread(self._client.warmup)
        return await asyncio.to_thread(self._client.session.get, url, timeout=30, allow_redirects=False,
                                       headers={"Accept-Language": "ru,en;q=0.9", "referer": "https://www.avito.ru/"})

    def _fmt_price(self, v: Optional[int]) -> str:
        if v is None:
            return "—"
        return f"{v:,}".replace(",", " ") + " ₽"

    async def _enrich_ad_details(self, ad: Ad):
        try:
            r = await self._avito_get(ad.url)
            if r.status_code != 200:
                return
            txt = r.text
            if txt:
                soup = BeautifulSoup(txt, "html.parser")

                # Имя продавца
                for selector in (
                    '[data-marker="seller-info/name"]',
                    '[data-marker="seller-link"]',
                    '.seller-info-name',
                    '.seller-link',
                ):
                    pe = soup.select_one(selector)
                    if pe:
                        ad.seller_name = _get_text(pe)
                        break

                # ID продавца
                m = re.search(r'"ownerId"\s*:\s*"?(?P<id>\d+)"?', txt)
                if m:
                    ad.seller_id = m.group("id")

                # Бейдж цены
                for b in ["Ниже рынка", "Хорошая цена", "Рыночная цена", "Выше рынка"]:
                    if b in txt:
                        ad.price_badge = b
                        break

                if "рассроч" in txt.lower() and "Рассрочка" not in ad.features:
                    ad.features.append("Рассрочка")

                # Верификация
                for selector in ('[data-marker="verified"]', '.verified-badge', '.is-verified'):
                    if soup.select_one(selector):
                        ad.is_verified = True
                        break

                # Просмотры
                for selector in ('[data-marker="total-views"]', '.item-views', '.views-count'):
                    t = soup.select_one(selector)
                    if t:
                        vm = re.search(r"\d+", _get_text(t))
                        if vm:
                            ad.views = int(vm.group())
                            break

                self._enrich_cache.add(ad.url)

        except Exception as e:
            logger.warning(f"Ошибка обогащения объявления {ad.url}: {e}")

    def _build_caption(self, ad: Ad) -> str:
        short = avito_short_url(ad.url)
        lines = [f"<b>{html.escape(ad.title)}</b>", ""]

        if ad.price is not None:
            price_line = f"💸 <b>{self._fmt_price(ad.price)}</b>"
            badge_parts: List[str] = []
            if ad.price_badge:
                badge_parts.append(f"✅ «{html.escape(ad.price_badge)}»")
            for ftr in ad.features:
                badge_parts.append(f"«{html.escape(ftr)}»")
            if badge_parts:
                price_line += " " + " ".join(badge_parts)
            lines.append(price_line)

        if ad.published_ts:
            lines.append("🗓 " + _fmt_dt(ad.published_ts))
        elif ad.date_str:
            lines.append("🗓 " + html.escape(ad.date_str))

        if ad.seller_name:
            icon = "🏪" if "магазин" in ad.seller_name.lower() else "👤"
            lines.append(f"{icon} {html.escape(ad.seller_name)}")

        if ad.seller_id:
            lines.append(f"🆔 {ad.seller_id}")

        if ad.views:
            lines.append(f"👁 {ad.views} просмотров")

        if ad.is_verified:
            lines.append("✅ Проверенный")

        lines.append("")
        lines.append(short)
        # Возвращаем с обычными переводами строк
        return "\n".join(lines)

    async def _fetch_ads(self) -> Optional[List[Ad]]:
        """Fetch the JSON feed with per-watcher and shared-route cooldowns."""
        now = time.monotonic()
        route_key = self._route_key()
        route_blocked_until = Watcher._route_blocked_until.get(route_key, 0.0)
        if route_blocked_until and route_blocked_until <= now:
            Watcher._route_blocked_until.pop(route_key, None)
            route_blocked_until = 0.0
        if now < max(self._blocked_until, route_blocked_until):
            return None
        if not self._api_url:
            api_url = await asyncio.to_thread(convert_url_to_api, self.url, API_URLS_FILE)
            if not api_url:
                logger.warning("Could not convert Avito URL to API URL: %s", self.url)
                self._blocked_until = time.monotonic() + 60
                return None
            self._api_url = api_url
        max_attempts = max(1, len(AVITO_PROXIES))
        if self._proxy_change_url():
            max_attempts = max(max_attempts, 2)
        for attempt in range(max_attempts):
            try:
                await self._wait_global_rate_limit()
                payload = await asyncio.to_thread(self._client.get_items, self._api_url)
                self.last_http_status = self._client.last_status
                self._consecutive_blocks = 0
                self.last_block_kind = None
                return [Ad(**item) for item in parse_api_items(payload, limit=20)]
            except AvitoBlock as block:
                blocked_route = self._route_key()
                self.last_http_status = block.status
                self.last_block_kind = block.kind
                wait = block.suggested_wait(self._consecutive_blocks)
                self._consecutive_blocks = min(self._consecutive_blocks + 1, 5)
                self._blocked_until = time.monotonic() + wait
                Watcher._route_blocked_until[blocked_route] = self._blocked_until
                route_changed = self._rotate_proxy()
                if route_changed and attempt + 1 < max_attempts:
                    self._blocked_until = 0
                    continue
                return None
            except AvitoHttpError as error:
                self.last_http_status = error.status
                if error.status in (400, 404, 410, 422):
                    try:
                        await asyncio.to_thread(invalidate_cached_api_url, self.url, API_URLS_FILE)
                    except Exception as cache_error:
                        logger.warning("Could not invalidate API URL cache: %s", cache_error)
                    self._api_url = None
                logger.error("Avito API error for %s: %s", self.url, error)
                self._blocked_until = time.monotonic() + min(60.0, self.interval_min)
                Watcher._route_blocked_until[self._route_key()] = self._blocked_until
                return None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failed_route = self._route_key()
                logger.error(f"Ошибка запроса к {self.url}: {e}")
                try:
                    if ADMIN_CHAT_ID is not None:
                        await self.bot.send_message(ADMIN_CHAT_ID, _br(f"Ошибка в Watcher: {e}"))
                except Exception:
                    pass
                route_changed = self._rotate_proxy()
                if route_changed and attempt + 1 < max_attempts:
                    await asyncio.sleep(2 + random.random() * 2)
                    continue
                self._blocked_until = time.monotonic() + min(60.0, self.interval_min)
                Watcher._route_blocked_until[failed_route] = self._blocked_until
        return None

    async def _prime_seen(self, limit=30):
        ads = await self._fetch_ads()
        if not ads:
            return
        for ad in ads[:limit]:
            self.seen[ad.ad_id] = time.time()

    def _ad_passes_filters(self, ad: Ad, sub: Subscription) -> bool:
        flt = sub.flt
        t = f"{ad.title}{ad.description}".lower()
        if flt.price_min is not None and (ad.price is None or ad.price < flt.price_min):
            return False
        if flt.price_max is not None and (ad.price is None or ad.price > flt.price_max):
            return False
        if flt.keywords_all and not all(w.lower() in t for w in flt.keywords_all):
            return False
        if flt.keywords_stop and any(w.lower() in t for w in flt.keywords_stop):
            return False
        return True

    def _bump_interval(self, found_new: bool):
        self._interval = max(self.interval_min, self._interval * 0.85) if found_new else min(self.interval_max, self._interval * 1.05)

    def _cleanup_seen(self, ttl=7 * 24 * 3600):
        now = time.time()
        if len(self.seen) > 20000:
            items = sorted(self.seen.items(), key=lambda kv: kv[1])
            for k, _ in items[: len(self.seen) // 2]:
                self.seen.pop(k, None)
        for k, ts in list(self.seen.items()):
            if now - ts > ttl:
                self.seen.pop(k, None)

    async def _run(self):
        app = cast(Any, self.bot).app
        lic: "LicenseManager" = app.license
        while self.has_subscribers():
            found_new = False
            ads_list = await self._fetch_ads()
            if ads_list:
                now_ts = time.time()
                for ad in ads_list:
                    if ad.ad_id in self.seen:
                        continue

                    delivered_any = False

                    for sub in list(self.subscribers.values()):
                        if not lic.is_active(sub.user_id):
                            logger.info("Пропуск %s для user=%s: лицензия неактивна", ad.ad_id, sub.user_id)
                            continue

                        # "Только новые" определяется watcher-ом через первичный
                        # снимок и seen, а не хрупкой датой Avito (она часто
                        # округляется до часа/дня).

                        if START_STRICT and ad.published_ts is not None and ad.published_ts + START_GRACE_SEC < sub.started_ts:
                            continue

                        if not self._ad_passes_filters(ad, sub):
                            logger.info("Пропуск %s для sub=%s: не прошёл фильтры (price=%s, title=%r)", ad.ad_id, sub.id, ad.price, ad.title)
                            continue

                        if app.sent_was_delivered(sub.user_id, ad.ad_id):
                            continue
                        if AVITO_ENRICH and ad.url not in self._enrich_cache:
                            await self._enrich_ad_details(ad)
                        caption = self._build_caption(ad)

                        chat_id = app.get_alert_chat_id(sub.user_id)
                        if chat_id and await app.send_to_alert(chat_id, caption, ad.image_url):
                            if self.on_deliver:
                                self.on_deliver(sub.user_id, ad)
                            app.sent_mark(sub.user_id, ad.ad_id, now_ts)  # per-user
                            delivered_any = True
                            found_new = True
                            logger.info("Отправлено объявление %s пользователю %s в alert_chat=%s", ad.ad_id, sub.user_id, chat_id)
                        else:
                            logger.warning("Не удалось отправить объявление %s: alert_chat=%s", ad.ad_id, chat_id)
                            if app.missing_alert_hint_once(sub.user_id):
                                link = app.alert_deeplink(sub.user_id)
                                kb = types.InlineKeyboardMarkup(inline_keyboard=[
                                    [types.InlineKeyboardButton(text="Подключить оповещения", url=link)]
                                ])
                                try:
                                    await self.bot.send_message(sub.user_id,
                                        _br("Чтобы получать объявления, подключите бота-оповещателя:"),
                                        reply_markup=kb, disable_web_page_preview=True)
                                except Exception:
                                    pass

                    if delivered_any:
                        self.seen[ad.ad_id] = now_ts
                    else:
                        # Не гоняем одну и ту же карточку по кругу, если она
                        # отфильтрована по возрасту/условиям подписки.
                        self.seen[ad.ad_id] = now_ts

            self._cleanup_seen()
            self._bump_interval(found_new)
            # Минимальная пауза между заходами
            await asyncio.sleep(max(1.0, self._interval) * random.uniform(0.9, 1.1))


@dataclass
class FeedItem:
    ts: float
    title: str
    price: Optional[int]
    url: str
    date_str: str


class WatcherManager:
    def __init__(self, bot: Bot, subscriptions_file: str = SUBSCRIPTIONS_FILE):
        self.bot = bot
        self.watchers: Dict[str, Watcher] = {}
        self.subs_by_user: Dict[int, List[Subscription]] = {}
        self._sub_id_seq = 1
        self.feed: Dict[int, Deque[FeedItem]] = {}
        self.subscriptions_file = subscriptions_file

    def list_user_subs(self, user_id: int):
        return self.subs_by_user.get(user_id, [])

    def _next_sub_id(self):
        v = self._sub_id_seq
        self._sub_id_seq += 1
        return v

    def _on_deliver(self, user_id: int, ad: Ad):
        d = self.feed.setdefault(user_id, deque(maxlen=50))
        d.appendleft(FeedItem(ts=time.time(), title=ad.title, price=ad.price, url=ad.url, date_str=ad.date_str))

    async def add_subscription(self, user_id: int, url: str, flt: Optional[SubscriberFilter] = None) -> Subscription:
        if flt is None:
            flt = SubscriberFilter()
        key = search_key_from_url(url)
        sub = Subscription(id=self._next_sub_id(), user_id=user_id, search_key=key, url=url, flt=flt)
        w = self.watchers.get(key)
        if not w:
            w = Watcher(search_key=key, url=url, bot=self.bot, on_deliver=self._on_deliver)
            self.watchers[key] = w
            await w.start()
        w.add_sub(sub)
        self.subs_by_user.setdefault(user_id, []).append(sub)
        self.save()
        return sub

    async def remove_subscription(self, user_id: int, sub_id: int) -> bool:
        subs = self.subs_by_user.get(user_id, [])
        for i, sub in enumerate(subs):
            if sub.id == sub_id:
                w = self.watchers.get(sub.search_key)
                if w:
                    w.remove_sub(sub.id)
                    if not w.has_subscribers():
                        await w.stop()
                        self.watchers.pop(sub.search_key, None)
                subs.pop(i)
                self.save()
                return True
        return False

    def get_sub_by_id(self, user_id: int, sub_id: int) -> Optional[Subscription]:
        for s in self.subs_by_user.get(user_id, []):
            if s.id == sub_id:
                return s
        return None

    def recent_feed(self, user_id: int, limit=10) -> List[FeedItem]:
        return list(self.feed.get(user_id, deque()))[:limit]

    def save(self) -> None:
        rows = []
        for subs in self.subs_by_user.values():
            for sub in subs:
                rows.append({
                    "id": sub.id, "user_id": sub.user_id, "search_key": sub.search_key,
                    "url": sub.url, "name": sub.name, "only_new": sub.only_new,
                    "started_ts": sub.started_ts, "filter": vars(sub.flt),
                })
        save_json(self.subscriptions_file, rows)

    async def restore(self) -> None:
        try:
            rows = load_json(self.subscriptions_file, [])
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Не удалось восстановить подписки: %s", exc)
            return
        for row in rows:
            try:
                flt = SubscriberFilter(**row.get("filter", {}))
                sub = Subscription(id=int(row["id"]), user_id=int(row["user_id"]),
                    search_key=str(row["search_key"]), url=str(row["url"]), flt=flt,
                    name=row.get("name"), only_new=bool(row.get("only_new", True)),
                    started_ts=float(row.get("started_ts", time.time())))
                self._sub_id_seq = max(self._sub_id_seq, sub.id + 1)
                self.subs_by_user.setdefault(sub.user_id, []).append(sub)
                watcher = self.watchers.get(sub.search_key)
                if not watcher:
                    watcher = Watcher(sub.search_key, sub.url, self.bot, self._on_deliver)
                    self.watchers[sub.search_key] = watcher
                watcher.add_sub(sub)
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Пропущена поврежденная подписка: %s", exc)
        for watcher in self.watchers.values():
            await watcher.start()


# === Главное меню ===
MAIN_INLINE_KB = types.InlineKeyboardMarkup(
    inline_keyboard=[
        [types.InlineKeyboardButton(text="🔎 Мои поиски", callback_data="searches"),
         types.InlineKeyboardButton(text="👤 Аккаунт", callback_data="account")],
        [types.InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help"),
         types.InlineKeyboardButton(text="Поддержка", callback_data="support")],
    ],
)

MAIN_MENU_TEXT = (
    "<b>Мониторинг Avito</b>\n"
    "Новые объявления приходят в отдельный бот-оповещатель."
)

HELP_TEXT = (
    "<b>Как начать</b>\n\n"
    "1. Активируйте ключ доступа.\n"
    "2. Подключите бот-оповещатель.\n"
    "3. Создайте поиск и укажите фильтры.\n\n"
    "Управление поисками доступно в разделе «Мои поиски»."
)


async def show_main_menu(
    message: types.Message,
    text: str = MAIN_MENU_TEXT,
    *,
    clear_reply_keyboard: bool = False,
) -> None:
    if clear_reply_keyboard:
        sent = await message.answer(text, reply_markup=types.ReplyKeyboardRemove())
        try:
            await sent.edit_reply_markup(reply_markup=MAIN_INLINE_KB)
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=MAIN_INLINE_KB)


# ===== Мастер добавления поиска =====
class SearchWizard(StatesGroup):
    url = State()
    price_min = State()
    price_max = State()
    name = State()


wizard_router = Router(name="wizard")

@wizard_router.message(F.text.in_(["/newsearch"]))
async def wizard_start(message: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(SearchWizard.url)
    await message.answer(
        "<b>Новый поиск · 1 из 3</b>\n\n"
        "Пришлите ссылку на результаты поиска Avito. Все нужные категории и параметры выберите на сайте заранее.",
        reply_markup=types.ReplyKeyboardRemove(),
        disable_web_page_preview=True,
    )

@wizard_router.message(SearchWizard.url, F.text.casefold() == "отмена")
@wizard_router.message(SearchWizard.url, F.text.casefold() == "назад")
@wizard_router.message(SearchWizard.url, F.text.in_(["📘 Инструкция", "🛟 Поддержка", "🧭 Поиски", "🔎 Мои поиски", "⚙️ Аккаунт", "👤 Аккаунт", "Аккаунт"]))
async def wizard_cancel_from_url(message: types.Message, state: FSMContext):
    await state.clear()
    await show_main_menu(message, clear_reply_keyboard=True)

@wizard_router.message(SearchWizard.url)
async def wizard_got_url(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    m = re.search(r"https?://\S+", raw)
    url_in = m.group(0) if m else raw
    if not is_valid_avito_url(url_in):
        await message.answer(_br("Похоже, это не ссылка Авито. Вставьте корректный URL."))
        return

    url = search_key_from_url(url_in)
    guessed = try_extract_filters_from_url(url)

    await state.update_data(url=url, guessed_kw=guessed.keywords_all)
    await state.set_state(SearchWizard.price_min)
    kw_txt = f"\n\nНайдены слова: <code>{html.escape(', '.join(guessed.keywords_all))}</code>" if guessed.keywords_all else ""
    await message.answer(
        "<b>Новый поиск · 2 из 3</b>\n\nМинимальная цена. Отправьте число или «-», если ограничения нет." + kw_txt,
        reply_markup=types.ReplyKeyboardMarkup(
            keyboard=[[types.KeyboardButton(text="-")], [types.KeyboardButton(text="Отмена")]],
            resize_keyboard=True
        ),
    )

@wizard_router.message(SearchWizard.price_min, F.text.casefold() == "отмена")
async def wizard_cancel_min(message: types.Message, state: FSMContext):
    await state.clear()
    await show_main_menu(message, clear_reply_keyboard=True)

@wizard_router.message(SearchWizard.price_min)
async def wizard_got_min(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    pmin = None
    if txt != "-":
        pmin = _parse_price_input(txt)
        if pmin is None:
            await message.answer(_br("Введите число или «-»."))
            return
    await state.update_data(price_min=pmin)
    await state.set_state(SearchWizard.price_max)
    await message.answer(
        "<b>Максимальная цена</b>\nВведите число. Если ограничения нет, оставьте 100 000 000.",
        reply_markup=types.ReplyKeyboardMarkup(
            keyboard=[[types.KeyboardButton(text="100000000")], [types.KeyboardButton(text="Отмена")]],
            resize_keyboard=True
        ),
    )

@wizard_router.message(SearchWizard.price_max, F.text.casefold() == "отмена")
async def wizard_cancel_max(message: types.Message, state: FSMContext):
    await state.clear()
    await show_main_menu(message, clear_reply_keyboard=True)

@wizard_router.message(SearchWizard.price_max)
async def wizard_got_max(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    pmax = _parse_price_input(txt)
    if pmax is None:
        await message.answer(_br("Максимальная цена обязательна. Введите число, например 100000000."))
        return

    data = await state.get_data()
    url = data.get("url") or ""
    pmin = data.get("price_min")
    kw = data.get("guessed_kw") or []

    await state.update_data(price_max=pmax)

    short_url = html.escape(avito_short_url(url), quote=True)
    min_text = f"{pmin:,}".replace(",", " ") + " ₽" if pmin is not None else "без ограничения"
    max_text = f"{pmax:,}".replace(",", " ") + " ₽"
    summary = [
        "<b>Новый поиск · 3 из 3</b>",
        "",
        f"Цена от: {min_text}",
        f"Цена до: {max_text}",
        f"Слова: {html.escape(', '.join(kw)) if kw else 'не заданы'}",
        f"<a href=\"{short_url}\">Открыть поиск на Avito</a>",
        "",
        "Отправьте короткое название, например «Samsung до 70 000».",
    ]
    await state.set_state(SearchWizard.name)
    await message.answer(_br("\\n".join(summary)), disable_web_page_preview=True)

@wizard_router.message(SearchWizard.name, F.text.casefold() == "отмена")
async def wizard_cancel_name(message: types.Message, state: FSMContext):
    await state.clear()
    await show_main_menu(message, clear_reply_keyboard=True)

@wizard_router.message(SearchWizard.name)
async def wizard_finish_create(message: types.Message, state: FSMContext):
    lic: LicenseManager = cast(Any, message.bot).app.license
    if not lic.is_active(message.chat.id):
        await message.answer(_br("Слот не активирован. Получите ключ у поддержки и отправьте его боту (формат «Ключ: xxxxx-…»)."))
        await state.clear()
        return

    data = await state.get_data()
    url = data.get("url") or ""
    pmin = data.get("price_min")
    pmax = data.get("price_max")
    kw = data.get("guessed_kw") or []
    name = (message.text or "").strip() or None

    flt = SubscriberFilter(price_min=pmin, price_max=pmax, keywords_all=kw)
    sub = await cast(Any, message.bot).app.manager.add_subscription(message.chat.id, url, flt)
    sub.name = name
    cast(Any, message.bot).app.manager.save()

    await state.clear()
    title = html.escape(name or f"Поиск №{sub.id}")
    await show_main_menu(
        message,
        f"✅ <b>{title}</b> создан\nНовые объявления будут приходить в бот-оповещатель.",
        clear_reply_keyboard=True,
    )


# ===== Поддержка =====
support_router = Router(name="support")

@support_router.message(F.text.in_(["🛟 Поддержка", "Поддержка"]))
async def support_info(message: types.Message, state: FSMContext):
    await state.clear()
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="Написать в поддержку", url=SUPPORT_LINK)],
        [types.InlineKeyboardButton(text="Назад в меню", callback_data="main_menu")],
    ])
    await message.answer(
        "<b>Поддержка</b>\n\nНапишите нам, если нужна помощь с доступом или настройкой поиска.",
        reply_markup=kb,
        disable_web_page_preview=True,
    )


@support_router.callback_query(F.data == "support")
async def support_callback(cq: types.CallbackQuery):
    if isinstance(cq.message, types.Message):
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="Написать в поддержку", url=SUPPORT_LINK)],
            [types.InlineKeyboardButton(text="Назад в меню", callback_data="main_menu")],
        ])
        await cq.message.edit_text(
            "<b>Поддержка</b>\n\nПоможем с доступом, привязкой оповещений и настройкой поиска.",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    await cq.answer()


@support_router.callback_query(F.data == "help")
async def help_callback(cq: types.CallbackQuery):
    if isinstance(cq.message, types.Message):
        await cq.message.edit_text(HELP_TEXT, reply_markup=MAIN_INLINE_KB)
    await cq.answer()


@support_router.callback_query(F.data == "main_menu")
async def main_menu_callback(cq: types.CallbackQuery):
    if isinstance(cq.message, types.Message):
        await cq.message.edit_text(MAIN_MENU_TEXT, reply_markup=MAIN_INLINE_KB)
    await cq.answer()


# ===== Экран «Поиски» =====
def build_searches_kb(
    subs: List[Subscription],
    lic: LicenseManager,
    user_id: int,
    watchers: Optional[Dict[str, "Watcher"]] = None,
) -> types.InlineKeyboardMarkup:
    rows: List[List[types.InlineKeyboardButton]] = []
    for s in subs:
        label = s.name or f"Поиск №{s.id}"
        status = get_watcher_status(s.search_key, watchers or {})
        rows.append([types.InlineKeyboardButton(
            text=f"{status}  {label}",
            callback_data=f"open_sub:{s.id}",
        )])
    if lic.is_active(user_id):
        rows.append([types.InlineKeyboardButton(text="＋ Создать поиск", callback_data="slot_new")])
    else:
        rows.append([types.InlineKeyboardButton(text="🔑 Получить доступ", callback_data="get_slot")])
    rows.append([types.InlineKeyboardButton(text="Назад в меню", callback_data="main_menu")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _short_wait(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} сек"
    minutes = max(1, (seconds + 59) // 60)
    if minutes < 60:
        return f"{minutes} мин"
    return f"{max(1, (minutes + 59) // 60)} ч"


def get_watcher_status(search_key: str, watchers: Optional[Dict[str, "Watcher"]]) -> str:
    w = (watchers or {}).get(search_key)
    if w is None:
        return "⚪"
    if w.task is None or w.task.done():
        return "⚪"

    now = time.monotonic()
    route_until = Watcher._route_blocked_until.get(w._route_key(), 0.0)
    wait = int(max(w._blocked_until, route_until) - now)
    if wait > 0:
        return f"🟡 {_short_wait(wait)}"
    if w.last_http_status and w.last_http_status != 200:
        return "🟡"
    return "🟢"

def format_sub_panel(sub: Subscription, lic: LicenseManager) -> str:
    title = html.escape(sub.name or f"Поиск №{sub.id}")
    exp = lic.expiry_dt(sub.user_id)
    price_min = f"{sub.flt.price_min:,}".replace(",", " ") + " ₽" if sub.flt.price_min is not None else "без минимума"
    price_max = f"{sub.flt.price_max:,}".replace(",", " ") + " ₽" if sub.flt.price_max is not None else "не задана"
    target = ", ".join(html.escape(word) for word in sub.flt.keywords_all) or "не заданы"
    stop = ", ".join(html.escape(word) for word in sub.flt.keywords_stop) or "не заданы"
    url = html.escape(avito_short_url(sub.url), quote=True)
    access = exp.strftime("%d.%m.%Y %H:%M") if exp else "не активен"
    return (
        f"🔎 <b>{title}</b>\n"
        f"Доступ до: <b>{access}</b>\n\n"
        f"<b>Фильтры</b>\n"
        f"Цена: <b>{price_min} — {price_max}</b>\n"
        f"Целевые слова: {target}\n"
        f"Стоп-слова: {stop}\n"
        f"Только новые: <b>{'включено' if sub.only_new else 'выключено'}</b>\n\n"
        f"<a href=\"{url}\">Открыть поиск на Avito</a>"
    )

def build_sub_inline_kb(sub: Subscription) -> types.InlineKeyboardMarkup:
    rid = sub.id
    rows = [
        [types.InlineKeyboardButton(text="Цена", callback_data=f"sub:{rid}:max"),
         types.InlineKeyboardButton(text="Целевые слова", callback_data=f"sub:{rid}:pos")],
        [types.InlineKeyboardButton(text="Стоп-слова", callback_data=f"sub:{rid}:stop"),
         types.InlineKeyboardButton(text=f"Только новые: {'вкл' if sub.only_new else 'выкл'}",
                                    callback_data=f"sub:{rid}:toggle_new")],
        [types.InlineKeyboardButton(text="Обновить сейчас", callback_data=f"force_update:{sub.id}")],
        [types.InlineKeyboardButton(text="Назад", callback_data="back_to_list"),
         types.InlineKeyboardButton(text="Удалить", callback_data=f"sub:{rid}:delete")],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


class EditPrice(StatesGroup):
    value = State()

class EditWords(StatesGroup):
    mode = State()
    sub_id = State()
    text = State()


searches_router = Router(name="searches")


def _searches_view(app: Any, user_id: int) -> tuple[str, types.InlineKeyboardMarkup]:
    subs = app.manager.list_user_subs(user_id)
    kb = build_searches_kb(subs, app.license, user_id, watchers=app.manager.watchers)
    if subs:
        text = "<b>Мои поиски</b>\nВыберите поиск для настройки."
    elif app.license.is_active(user_id):
        text = "<b>Мои поиски</b>\nУ вас пока нет настроенных поисков."
    else:
        text = "<b>Мои поиски</b>\nДля создания поиска нужен активный доступ."
    return text, kb


async def _send_searches_screen(message: types.Message, user_id: int) -> None:
    app = cast(Any, message.bot).app
    text, kb = _searches_view(app, user_id)
    await message.answer(text, reply_markup=kb)


async def _edit_searches_screen(message: types.Message, user_id: int) -> None:
    app = cast(Any, message.bot).app
    text, kb = _searches_view(app, user_id)
    await message.edit_text(text, reply_markup=kb)

@searches_router.message(F.text.in_(["🧭 Поиски", "🔎 Мои поиски", "Поиски"]))
async def searches_screen(message: types.Message, state: FSMContext):
    await state.clear()
    await _send_searches_screen(message, message.chat.id)

@searches_router.callback_query(F.data == "searches")
async def cb_menu_searches(cq: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if isinstance(cq.message, types.Message):
        await _edit_searches_screen(cq.message, cq.from_user.id)
    await cq.answer()

@searches_router.callback_query(F.data.startswith("force_update:"))
async def cb_force_update(cq: types.CallbackQuery):
    data_str: str = cq.data or ""
    try:
        sub_id = int(data_str.split(":", 1)[1])
    except (IndexError, ValueError):
        await cq.answer(); return

    app = cast(Any, cq.bot).app
    sub = app.manager.get_sub_by_id(cq.from_user.id, sub_id)
    if not sub:
        await cq.answer("Поиск не найден", show_alert=True); return

    watcher = app.manager.watchers.get(sub.search_key)
    if not watcher:
        await cq.answer("Вотчер не запущен", show_alert=True); return

    try:
        await watcher.stop()
        await watcher.start()
    except Exception as exc:
        logger.warning("force_update failed for %s: %s", sub.search_key, exc)
        await cq.answer("Не удалось перезапустить", show_alert=True); return

    await cq.answer("Проверка запущена")

@searches_router.callback_query(F.data == "get_slot")
async def cb_get_slot(cq: types.CallbackQuery):
    if isinstance(cq.message, types.Message):
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="Написать в поддержку", url=SUPPORT_LINK)],
            [types.InlineKeyboardButton(text="Назад к поискам", callback_data="back_to_list")],
        ])
        await cq.message.edit_text(
            "<b>Доступ к мониторингу</b>\n\n"
            "Получите ключ у поддержки, затем отправьте его в этот чат.",
            reply_markup=kb,
            disable_web_page_preview=True
        )
    await cq.answer()

@searches_router.callback_query(F.data == "slot_new")
async def cb_slot_new(cq: types.CallbackQuery, state: FSMContext):
    if not isinstance(cq.message, types.Message):
        await cq.answer(); return
    await state.clear()
    await state.set_state(SearchWizard.url)
    await cq.message.answer(
        "<b>Новый поиск · 1 из 3</b>\n\n"
        "Пришлите ссылку на результаты поиска Avito. Все нужные категории и параметры выберите на сайте заранее.",
        reply_markup=types.ReplyKeyboardRemove(),
        disable_web_page_preview=True,
    )
    await cq.answer()

@searches_router.callback_query(F.data == "close_menu")
async def cb_close_menu(cq: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if isinstance(cq.message, types.Message):
        try:
            await cq.message.delete()
        except Exception:
            try:
                await cq.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
    await cq.answer("Закрыто")

@searches_router.callback_query(F.data == "back_to_list")
async def cb_back_to_list(cq: types.CallbackQuery):
    if isinstance(cq.message, types.Message):
        await _edit_searches_screen(cq.message, cq.from_user.id)
    await cq.answer()

@searches_router.callback_query(F.data.startswith("open_sub:"))
async def cb_open_sub(cq: types.CallbackQuery, state: FSMContext):
    data_str: str = cq.data or ""
    try:
        sub_id = int(data_str.split(":", 1)[1])
    except Exception:
        await cq.answer(); return
    mng = cast(Any, cq.bot).app.manager
    sub = mng.get_sub_by_id(cq.from_user.id, sub_id)
    if not sub:
        await cq.answer("Подписка не найдена", show_alert=True); return
    if isinstance(cq.message, types.Message):
        lic: LicenseManager = cast(Any, cq.bot).app.license
        await cq.message.edit_text(
            format_sub_panel(sub, lic),
            reply_markup=build_sub_inline_kb(sub),
            disable_web_page_preview=True,
        )
    await cq.answer()

@searches_router.callback_query(F.data.startswith("sub:"))
async def cb_sub_actions(cq: types.CallbackQuery, state: FSMContext):
    data_str: str = cq.data or ""
    m = re.match(r"sub:(\d+):(\w+)", data_str)
    if not m:
        await cq.answer(); return
    sub_id = int(m.group(1))
    action = m.group(2)
    mng = cast(Any, cq.bot).app.manager
    sub = mng.get_sub_by_id(cq.from_user.id, sub_id)
    if not sub:
        await cq.answer("Подписка не найдена", show_alert=True); return

    if action == "max":
        await state.set_state(EditPrice.value)
        await state.update_data(field="max", sub_id=sub.id)
        if isinstance(cq.message, types.Message):
            await cq.message.answer("Введите новую максимальную цену числом.")
        await cq.answer(); return

    if action in ("pos", "stop"):
        await state.set_state(EditWords.text)
        await state.update_data(mode=action, sub_id=sub.id)
        if isinstance(cq.message, types.Message):
            hint = "целевые слова" if action == "pos" else "стоп-слова"
            curr = ", ".join(sub.flt.keywords_all if action == "pos" else sub.flt.keywords_stop) or "—"
            await cq.message.answer(
                f"Введите {hint} через запятую. Чтобы очистить список, отправьте «-».\n"
                f"Сейчас: <code>{html.escape(curr)}</code>"
            )
        await cq.answer(); return

    if action == "toggle_new":
        sub.only_new = not sub.only_new
        mng.save()
        if isinstance(cq.message, types.Message):
            lic: LicenseManager = cast(Any, cq.bot).app.license
            await cq.message.edit_text(
                format_sub_panel(sub, lic),
                reply_markup=build_sub_inline_kb(sub),
                disable_web_page_preview=True,
            )
        await cq.answer("Обновлено"); return

    if action == "delete":
        if isinstance(cq.message, types.Message):
            title = html.escape(sub.name or f"Поиск №{sub.id}")
            kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="Удалить поиск", callback_data=f"sub:{sub.id}:delete_confirm")],
                [types.InlineKeyboardButton(text="Отмена", callback_data=f"sub:{sub.id}:delete_cancel")],
            ])
            await cq.message.edit_text(
                f"<b>Удалить «{title}»?</b>\nЭто действие нельзя отменить.",
                reply_markup=kb,
            )
        await cq.answer(); return

    if action == "delete_cancel":
        if isinstance(cq.message, types.Message):
            lic: LicenseManager = cast(Any, cq.bot).app.license
            await cq.message.edit_text(
                format_sub_panel(sub, lic),
                reply_markup=build_sub_inline_kb(sub),
                disable_web_page_preview=True,
            )
        await cq.answer(); return

    if action == "delete_confirm":
        ok = await mng.remove_subscription(cq.from_user.id, sub.id)
        if isinstance(cq.message, types.Message):
            text, kb = _searches_view(cast(Any, cq.bot).app, cq.from_user.id)
            notice = "Поиск удалён.\n\n" if ok else "Не удалось удалить поиск.\n\n"
            await cq.message.edit_text(notice + text, reply_markup=kb)
        await cq.answer(); return

    await cq.answer()

# ====== Применение изменений из FSM ======
@searches_router.message(EditPrice.value)
async def ui_edit_apply_max(m: types.Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("field")
    raw = data.get("sub_id")
    if raw is None:
        await state.clear()
        await m.reply(_br("Ошибка: нет идентификатора поиска."), reply_markup=MAIN_INLINE_KB)
        return
    sub_id = int(raw)

    txt = (m.text or "").strip()
    mng = cast(Any, m.bot).app.manager
    sub = mng.get_sub_by_id(m.chat.id, sub_id)
    if not sub:
        await state.clear()
        await m.reply(_br("Подписка не найдена."), reply_markup=MAIN_INLINE_KB)
        return
    if field == "max":
        parsed = _parse_price_input(txt)
        if parsed is None:
            await m.reply(_br("Максимальная цена должна быть числом, например 100000000."))
            return
        sub.flt.price_max = parsed
        mng.save()
    await state.clear()
    lic: LicenseManager = cast(Any, m.bot).app.license
    await m.reply(
        format_sub_panel(sub, lic),
        reply_markup=build_sub_inline_kb(sub),
        disable_web_page_preview=True,
    )

@searches_router.message(EditWords.text)
async def ui_edit_words_apply(m: types.Message, state: FSMContext):
    data = await state.get_data()
    mode = (data.get("mode") or "pos")
    raw = data.get("sub_id")
    if raw is None:
        await state.clear()
        await m.reply(_br("Ошибка: нет идентификатора поиска."), reply_markup=MAIN_INLINE_KB)
        return
    sub_id = int(raw)

    mng = cast(Any, m.bot).app.manager
    sub = mng.get_sub_by_id(m.chat.id, sub_id)
    if not sub:
        await state.clear()
        await m.reply(_br("Подписка не найдена."), reply_markup=MAIN_INLINE_KB)
        return
    raw_words = (m.text or "").strip()
    words = [] if raw_words == "-" else [w.strip() for w in raw_words.replace(";", ",").split(",") if w.strip()]
    if mode == "pos":
        sub.flt.keywords_all = words
    else:
        sub.flt.keywords_stop = words
    mng.save()
    await state.clear()
    lic: LicenseManager = cast(Any, m.bot).app.license
    await m.reply(
        format_sub_panel(sub, lic),
        reply_markup=build_sub_inline_kb(sub),
        disable_web_page_preview=True,
    )


# ===== Приём ключа (устойчивый) =====
key_router = Router(name="keys")

async def _accept_key_impl(m: types.Message, state: FSMContext):
    app = cast(Any, m.bot).app
    lic: LicenseManager = app.license

    txt = (m.text or "").strip()
    m_uuid = KEY_RE.search(txt)  # <-- ИЩЕМ ВЕЗДЕ, не только в начале
    if not m_uuid:
        return

    key_value = m_uuid.group(1)
    try:
        uuid.UUID(key_value)
    except Exception:
        await m.reply(_br("Ключ недействителен."))
        return

    res = app.redeem_key(key_value)
    if not res:
        await state.clear()
        await m.reply(_br("Ключ недействителен или уже использован."))
        return

    hours, expires_ts = res  # expires_ts формируется в момент активации!
    logger.info(f"User {m.chat.id} activated key: {key_value}")
    await state.clear()
    lic.activate_until(m.chat.id, expires_ts)
    exp_str = _fmt_dt(expires_ts)

    # учёт аккаунта
    app.account_register_if_needed(m.chat.id)
    app.account_add_key(m.chat.id, key_value, hours, expires_ts)

    # Кнопка для привязки alert-бота
    deeplink = app.alert_deeplink(m.chat.id)
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="1. Подключить оповещения", url=deeplink)],
            [types.InlineKeyboardButton(text="2. Создать поиск", callback_data="slot_new")],
        ]
    )

    await m.reply(
        f"✅ <b>Доступ активирован</b>\nДействует до: <b>{exp_str}</b>\n\n"
        "Сначала подключите оповещения, затем создайте поиск.",
        disable_web_page_preview=True,
        reply_markup=kb,
    )

# Матчим как «Ключ: …», так и просто UUID
@key_router.message(F.text.regexp(r"(?i)(?:^|\s)(?:ключ\s*:\s*)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:\s|$)"))
async def accept_key_regexp(m: types.Message, state: FSMContext):
    await _accept_key_impl(m, state)


# ===== Аккаунт =====
account_router = Router(name="account")

def account_panel_text(app: Any, user_id: int) -> str:
    lic: LicenseManager = app.license
    acc = app.account_get(user_id)
    subs = app.manager.list_user_subs(user_id)
    exp_dt = lic.expiry_dt(user_id)

    key_txt = "—"
    if acc and acc.get("keys"):
        now = time.time()
        current = None
        for k in sorted(acc["keys"], key=lambda x: x.get("activated", 0), reverse=True):
            if float(k.get("expires", 0) or 0) > now:
                current = k; break
        if not current:
            current = sorted(acc["keys"], key=lambda x: x.get("activated", 0), reverse=True)[0]
        key_value = str(current.get("key", ""))
        key_txt = f"•••• {key_value[-4:]}" if key_value else "—"

    lines = [
        "👤 <b>Аккаунт</b>",
        f"ID: <code>{user_id}</code>",
        f"Регистрация: {_fmt_dt((acc or {}).get('registered')) if acc else '—'}",
        "",
        f"Ключ: <code>{key_txt}</code>",
        f"Доступ до: <b>{exp_dt.strftime('%d.%m.%Y %H:%M') if exp_dt else 'не активен'}</b>",
        f"Поисков: <b>{len(subs)}</b>",
    ]
    return "\n".join(lines)

def build_account_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="История ключей", callback_data="account:expired"),
             types.InlineKeyboardButton(text="Продлить доступ", url=SUPPORT_LINK)],
            [types.InlineKeyboardButton(text="Назад в меню", callback_data="main_menu")],
        ]
    )

@account_router.message(F.text.in_(["⚙️ Аккаунт", "👤 Аккаунт", "Аккаунт", "/account"]))
async def account_show(m: types.Message, state: FSMContext):
    app = cast(Any, m.bot).app
    app.account_register_if_needed(m.chat.id)
    txt = account_panel_text(app, m.chat.id)
    await m.answer(txt, reply_markup=build_account_kb())


@account_router.callback_query(F.data == "account")
async def account_callback(cq: types.CallbackQuery):
    app = cast(Any, cq.bot).app
    app.account_register_if_needed(cq.from_user.id)
    if isinstance(cq.message, types.Message):
        await cq.message.edit_text(
            account_panel_text(app, cq.from_user.id),
            reply_markup=build_account_kb(),
        )
    await cq.answer()

@account_router.callback_query(F.data == "account:close")
async def account_close(cq: types.CallbackQuery):
    if isinstance(cq.message, types.Message):
        try:
            await cq.message.delete()
        except Exception:
            pass
    await cq.answer()

@account_router.callback_query(F.data == "account:expired")
async def account_expired(cq: types.CallbackQuery):
    app = cast(Any, cq.bot).app
    acc = app.account_get(cq.from_user.id) or {}
    items = []
    now = time.time()
    for k in (acc.get("keys") or []):
        exp_ts = float(k.get("expires") or 0)
        if exp_ts and exp_ts < now:
            items.append(f"• <code>{k.get('key','')}</code> — истёк {_fmt_dt(exp_ts)}")
    text = "Истёкших ключей не найдено." if not items else "<b>Истёкшие ключи:</b>\\n" + "\\n".join(items)
    if isinstance(cq.message, types.Message):
        await cq.message.answer(_br(text))
    await cq.answer()


# ===== Главное приложение =====
class App:
    def __init__(self, token: str):
        self.bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        cast(Any, self.bot).app = self
        self.dp = Dispatcher(storage=MemoryStorage())
        self.manager = WatcherManager(self.bot)
        self.license = LicenseManager()

        # alert
        self.alert_token: Optional[str] = ALERT_BOT_TOKEN
        self.alert_username: str = ALERT_BOT_USERNAME
        self.bindings_file: str = BINDINGS_FILE
        self.alert_links_file: str = ALERT_LINKS_FILE
        self._alert_warned: Set[int] = set()

        # storage
        self.keys_file: str = KEYS_FILE
        self.sent_file: str = SENT_FILE
        self.accounts_file: str = ACCOUNTS_FILE

        self._restore_licenses()
        self._register()

    # ===== работа с привязками main_user_id -> alert_chat_id
    def _load_bindings(self) -> Dict[str, int]:
        if not os.path.exists(self.bindings_file):
            return {}
        try:
            data = load_json(self.bindings_file, {}) or {}
            return {str(k): int(v) for k, v in data.items()}
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Не удалось прочитать привязки: %s", exc)
            return {}

    def _save_bindings(self, data: Dict[str, int]) -> None:
        save_json(self.bindings_file, data)

    def get_alert_chat_id(self, main_user_id: int) -> Optional[int]:
        b = self._load_bindings()
        val = b.get(str(main_user_id))
        return int(val) if val else None

    def alert_deeplink(self, main_user_id: int) -> str:
        uname = self.alert_username or "<УКАЖИТЕ_ALERT_BOT_USERNAME>"
        token = secrets.token_urlsafe(24)
        now = time.time()
        def add_link(links):
            expired = [k for k, v in links.items() if float(v.get("expires", 0)) < now]
            for key in expired:
                del links[key]
            links[token] = {"user_id": main_user_id, "expires": now + 600}
        update_json(self.alert_links_file, {}, add_link)
        return f"https://t.me/{uname}?start={token}"

    def missing_alert_hint_once(self, user_id: int) -> bool:
        if user_id in self._alert_warned:
            return False
        self._alert_warned.add(user_id)
        return True

    async def send_to_alert(self, alert_chat_id: int, caption: str, image_url: Optional[str]) -> bool:
        """Отправка в alert-бота через его Bot API. Заменяем любые <br> на \\n для безопасности."""
        if not self.alert_token:
            return False
        api = f"https://api.telegram.org/bot{self.alert_token}"
        safe_caption = _br(caption)
        try:
            async with aiohttp.ClientSession() as s:
                if image_url:
                    form = aiohttp.FormData()
                    form.add_field("chat_id", str(alert_chat_id))
                    form.add_field("caption", safe_caption)
                    form.add_field("parse_mode", "HTML")
                    form.add_field("photo", image_url)
                    async with s.post(f"{api}/sendPhoto", data=form, timeout=10) as r:
                        return (r.status == 200)
                else:
                    payload = {"chat_id": alert_chat_id, "text": safe_caption, "parse_mode": "HTML", "disable_web_page_preview": True}
                    async with s.post(f"{api}/sendMessage", json=payload, timeout=10) as r:
                        return (r.status == 200)
        except Exception:
            return False

    # ===== ключи =====
    def _load_keys(self) -> Dict[str, Dict[str, Any]]:
        if not os.path.exists(self.keys_file):
            return {}
        try:
            data = load_json(self.keys_file, {}) or {}
            return {str(k): dict(v) for k, v in data.items()}
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Не удалось прочитать ключи: %s", exc)
            return {}

    def _save_keys(self, data: Dict[str, Dict[str, Any]]) -> None:
        save_json(self.keys_file, data)

    def issue_key(self, hours: int = 24, uses: int = 1) -> str:
        k = str(uuid.uuid4())
        update_json(self.keys_file, {}, lambda data: data.__setitem__(
            k, {"hours": int(hours), "uses_left": int(uses), "created": time.time()}))
        return k

    def redeem_key(self, key: str) -> Optional[tuple[int, float]]:
        """
        Возвращает (hours, expires_ts). ВАЖНО: expires_ts формируется в МОМЕНТ АКТИВАЦИИ,
        то есть ровно «сейчас + hours*3600», независимо от времени выдачи ключа.
        """
        def redeem(data):
            rec = data.get(key)
            if not rec or int(rec.get("uses_left", 0)) <= 0:
                return None
            hours = int(rec.get("hours", 24))
            expires_ts = time.time() + hours * 3600
            rec["uses_left"] = int(rec["uses_left"]) - 1
            return hours, float(expires_ts)
        return update_json(self.keys_file, {}, redeem)

    # ===== антидубликаты per-user + global =====
    def _load_sent(self) -> Dict[str, Dict[str, float]]:
        if not os.path.exists(self.sent_file):
            return {}
        try:
            data = load_json(self.sent_file, {}) or {}
            out: Dict[str, Dict[str, float]] = {}
            for uk, mp in data.items():
                if isinstance(mp, dict):
                    out[str(uk)] = {str(aid): float(ts) for aid, ts in mp.items()}
            return out
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Не удалось прочитать dedup: %s", exc)
            return {}

    def _save_sent(self, data: Dict[str, Dict[str, float]]) -> None:
        save_json(self.sent_file, data)

    def _cleanup_sent_locked(self, data: Dict[str, Dict[str, float]]) -> None:
        cutoff = time.time() - DEDUP_TTL_DAYS * 86400
        for uk in list(data.keys()):
            inner = data.get(uk, {})
            for ad_id in list(inner.keys()):
                if inner[ad_id] < cutoff:
                    del inner[ad_id]
            if not inner:
                del data[uk]

    def sent_was_delivered(self, user_id: int, ad_id: str) -> bool:
        data = self._load_sent()
        inner = data.get(str(user_id), {})
        return ad_id in inner

    def sent_mark(self, user_id: int, ad_id: str, ts: Optional[float] = None) -> None:
        def mark(data):
            data.setdefault(str(user_id), {})[ad_id] = float(ts or time.time())
            self._cleanup_sent_locked(data)
        update_json(self.sent_file, {}, mark)

    def dedup_global_enabled(self) -> bool:
        return DEDUP_GLOBAL

    def sent_global_was_delivered(self, ad_id: str) -> bool:
        data = self._load_sent()
        inner = data.get("_global", {})
        return ad_id in inner

    def sent_global_mark(self, ad_id: str, ts: Optional[float] = None) -> None:
        def mark(data):
            data.setdefault("_global", {})[ad_id] = float(ts or time.time())
            self._cleanup_sent_locked(data)
        update_json(self.sent_file, {}, mark)

    # ===== аккаунты =====
    def _load_accounts(self) -> Dict[str, Dict[str, Any]]:
        if not os.path.exists(self.accounts_file):
            return {}
        try:
            return load_json(self.accounts_file, {}) or {}
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Не удалось прочитать аккаунты: %s", exc)
            return {}

    def _save_accounts(self, data: Dict[str, Dict[str, Any]]) -> None:
        save_json(self.accounts_file, data)

    def _restore_licenses(self) -> None:
        now = time.time()
        for user_id, account in self._load_accounts().items():
            expires = max((float(k.get("expires", 0)) for k in account.get("keys", [])), default=0)
            if expires > now:
                self.license.activate_until(int(user_id), expires)

    def account_register_if_needed(self, user_id: int) -> None:
        def register(data):
            data.setdefault(str(user_id), {"registered": time.time(), "keys": []})
        update_json(self.accounts_file, {}, register)

    def account_add_key(self, user_id: int, key_value: str, hours: int, exp_ts: Optional[float]):
        def add_key(data):
            u = data.setdefault(str(user_id), {"registered": time.time(), "keys": []})
            u.setdefault("keys", []).append({"key": key_value, "activated": time.time(),
                "hours": int(hours), "expires": float(exp_ts or 0.0)})
        update_json(self.accounts_file, {}, add_key)

    def account_get(self, user_id: int) -> Optional[Dict[str, Any]]:
        return self._load_accounts().get(str(user_id))

    def _register(self):
        # Регистрируем key_router ПЕРВЫМ — чтобы сообщения с ключами обрабатывались гарантированно
        self.dp.include_router(key_router)
        self.dp.include_router(wizard_router)
        self.dp.include_router(support_router)
        self.dp.include_router(searches_router)
        self.dp.include_router(account_router)

        @self.dp.message(Command("test_alert"))
        async def test_alert(m: types.Message):
            if ADMIN_CHAT_ID is None or m.chat.id != ADMIN_CHAT_ID:
                await m.reply(_br("Команда доступна только админу.")); return
            chat_id = self.get_alert_chat_id(m.chat.id)
            if not chat_id:
                await m.reply(_br("Alert binding не найден. Нажмите кнопку привязки после активации ключа.")); return
            ok = await self.send_to_alert(chat_id, _br("Тестовое сообщение из основного бота ✅"), None)
            await m.reply(_br("Отправлено." if ok else "Не удалось отправить (проверь токен ALERT_BOT_TOKEN и привязку)."))

        @self.dp.message(F.text == "Назад")
        async def back_btn(m: types.Message, state: FSMContext):
            await state.clear()
            await show_main_menu(m, clear_reply_keyboard=True)

        @self.dp.message(F.text.in_(["📘 Инструкция", "Инструкция", "/help"]))
        async def help_cmd(m: types.Message):
            self.account_register_if_needed(m.chat.id)
            await m.answer(
                HELP_TEXT,
                reply_markup=MAIN_INLINE_KB,
                disable_web_page_preview=True
            )

        @self.dp.message(Command("start"))
        async def start_cmd(m: types.Message):
            self.account_register_if_needed(m.chat.id)
            await show_main_menu(m, clear_reply_keyboard=True)

        @self.dp.message(Command("add"))
        async def add_cmd(m: types.Message):
            self.account_register_if_needed(m.chat.id)
            lic: LicenseManager = self.license
            if not lic.is_active(m.chat.id):
                await m.reply(_br("Слот не активирован. Получите ключ у поддержки и отправьте его боту (формат «Ключ: …»)."))
                return
            txt = (m.text or "")
            parts = txt.split(maxsplit=2)
            if len(parts) < 2:
                await m.reply(_br("Укажите ссылку после /add. Пример:\\n/add https://www.avito.ru/... max=100000000,min=10000"))
                return
            url = parts[1].strip()
            if not is_valid_avito_url(url):
                await m.reply(_br("Разрешены только ссылки https://avito.ru и его поддоменов."))
                return
            params = parts[2].strip() if len(parts) >= 3 else ""
            flt = try_extract_filters_from_url(url)
            if params:
                p = params.replace(" ", "")
                for token in p.split(","):
                    if not token:
                        continue
                    if token.startswith("kw="):
                        kws = token[3:].replace(";", ",").replace("|", ",")
                        flt.keywords_all = [w for w in kws.split(",") if w]
                    elif token.startswith("min="):
                        try:
                            flt.price_min = int(token[4:])
                        except Exception:
                            pass
                    elif token.startswith("max="):
                        try:
                            flt.price_max = int(token[4:])
                        except Exception:
                            pass
            if flt.price_max is None:
                await m.reply(_br("⚠️ Укажите максимальную цену (параметр <code>max=</code>) или воспользуйтесь мастером «/newsearch»."))
                return
            sub = await self.manager.add_subscription(m.chat.id, url, flt)
            await m.reply(
                _br(f"Подписка добавлена: <b>{sub.id}</b>\\nСсылка: {url}\\n"
                    "Карточки будут приходить в бот-оповещатель.")
            )

        @self.dp.message(Command("list"))
        async def list_cmd(m: types.Message):
            self.account_register_if_needed(m.chat.id)
            subs = self.manager.list_user_subs(m.chat.id)
            if not subs:
                await m.reply(_br("У вас нет активных подписок.")); return
            lines = ["Ваши подписки:"]
            for s in subs:
                desc = []
                if s.name: desc.append(f"name={s.name}")
                if s.flt.keywords_all: desc.append(f"kw={','.join(s.flt.keywords_all)}")
                if s.flt.keywords_stop: desc.append(f"stop={','.join(s.flt.keywords_stop)}")
                if s.flt.price_min is not None: desc.append(f"min={s.flt.price_min}")
                if s.flt.price_max is not None: desc.append(f"max={s.flt.price_max}")
                lines.append(f"{s.id}: {s.url} " + (f"({' ; '.join(desc)})" if desc else ""))
            lines.append("\\nПодсказка: карточки приходят в бот-оповещатель после привязки.")
            await m.reply(_br("\\n".join(lines)), disable_web_page_preview=True)

        @self.dp.message(Command("remove"))
        async def remove_cmd(m: types.Message):
            self.account_register_if_needed(m.chat.id)
            parts = (m.text or "").split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip().isdigit():
                await m.reply(_br("Укажите ID: /remove 3")); return
            ok = await self.manager.remove_subscription(m.chat.id, int(parts[1].strip()))
            await m.reply(_br("Удалено." if ok else "Подписка не найдена."))

        @self.dp.message(Command("genkey"))
        async def genkey_cmd(m: types.Message):
            if ADMIN_CHAT_ID is None or m.chat.id != ADMIN_CHAT_ID:
                await m.reply(_br("Команда доступна только админу.")); return
            parts = (m.text or "").split()
            hours = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 24
            uses = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 1
            key = self.issue_key(hours=hours, uses=uses)
            await m.reply(
                _br(
                    "Сгенерирован ключ ✅\\n"
                    f"Ключ: <code>{key}</code>\\n"
                    f"Срок: {hours} ч\\n"
                    f"Использований: {uses}\\n\\n"
                    "Отправьте пользователю в виде: <b>Ключ: </b><code>значение</code>"
                )
            )

        @self.dp.message(Command("diag"))
        async def diag_cmd(m: types.Message):
            if ADMIN_CHAT_ID is None or m.chat.id != ADMIN_CHAT_ID:
                await m.reply(_br("Команда доступна только админу.")); return
            lic = "✅" if self.license.is_active(m.chat.id) else "❌"
            bind = self.get_alert_chat_id(m.chat.id)
            subs = self.manager.list_user_subs(m.chat.id)
            watchers = list(self.manager.watchers.items())
            lines = [
                "<b>Диагностика</b>",
                f"Лицензия: {lic}",
                f"Alert binding: {'✅ '+str(bind) if bind else '❌'}",
                f"Подписок у этого юзера: {len(subs)}",
                f"Активных вотчеров: {len(watchers)}",
            ]
            if watchers:
                lines.append("Ключи вотчеров:")
                now_mono = time.monotonic()
                for key, watcher in watchers:
                    route_until = Watcher._route_blocked_until.get(watcher._route_key(), 0.0)
                    cooldown = max(0, int(max(watcher._blocked_until, route_until) - now_mono))
                    status = str(watcher.last_http_status or "-")
                    block = watcher.last_block_kind or "ok"
                    consec = watcher._consecutive_blocks
                    lines.append(
                        f"• {html.escape(key)[:55]} | http={status} | {html.escape(block)} | wait={cooldown}s | consec={consec}"
                    )

            await m.reply(_br("\\n".join(lines)), disable_web_page_preview=True)

        @self.dp.message(Command("dedup_clear"))
        async def dedup_clear_cmd(m: types.Message):
            if ADMIN_CHAT_ID is None or m.chat.id != ADMIN_CHAT_ID:
                await m.reply(_br("Команда доступна только админу.")); return
            parts = (m.text or "").split(maxsplit=1)
            target = parts[1].strip().lower() if len(parts) > 1 else ""
            data = self._load_sent()
            if not target or target == "all":
                data = {}; self._save_sent(data)
                await m.reply(_br("Антидубликат очищен полностью (per-user и global).")); return
            if target == "global":
                if "_global" in data: del data["_global"]
                self._save_sent(data); await m.reply(_br("Глобальный антидубликат очищен.")); return
            if target.isdigit():
                if target in data:
                    del data[target]; self._save_sent(data)
                    await m.reply(_br(f"Антидубликат очищен для user_id={target}."))
                else:
                    await m.reply(_br(f"Для user_id={target} записей не было."))
                return
            await m.reply(_br("Формат: /dedup_clear [all|global|<user_id>]"))

    async def setup_menu(self):
        await self.bot.set_my_commands(
            [
                types.BotCommand(command="start", description="Главное меню"),
                types.BotCommand(command="newsearch", description="Создать поиск"),
                types.BotCommand(command="account", description="Аккаунт"),
                types.BotCommand(command="help", description="Помощь"),
            ]
        )
        if ADMIN_CHAT_ID is not None:
            await self.bot.set_my_commands(
                [
                    types.BotCommand(command="start", description="Главное меню"),
                    types.BotCommand(command="newsearch", description="Создать поиск"),
                    types.BotCommand(command="account", description="Аккаунт"),
                    types.BotCommand(command="genkey", description="Выдать ключ"),
                    types.BotCommand(command="diag", description="Диагностика"),
                    types.BotCommand(command="dedup_clear", description="Очистить антидубликат"),
                ],
                scope=types.BotCommandScopeChat(chat_id=ADMIN_CHAT_ID),
            )


# ===== main =====
async def main():
    load_dotenv()
    keep_alive()  # no-op если нет
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN отсутствует в .env/Secrets")
    app = App(token)
    await app.manager.restore()
    await app.setup_menu()
    await app.dp.start_polling(app.bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
