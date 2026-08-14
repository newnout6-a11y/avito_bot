"""Polling workers and subscription persistence for the Avito monitor."""

import asyncio
import html
import logging
import random
import re
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, cast

from aiogram import Bot, types
from bs4 import BeautifulSoup

from avito_api import (
    AvitoBlock,
    AvitoHttpClient,
    AvitoHttpError,
    convert_url_to_api,
    invalidate_cached_api_url,
    parse_api_items,
)
from avito_domain import (
    Ad,
    FeedItem,
    SubscriberFilter,
    Subscription,
    _br,
    _fmt_dt,
    _get_text,
    avito_short_url,
    search_key_from_url,
)
from avito_settings import (
    ADMIN_CHAT_ID,
    API_URLS_FILE,
    AVITO_ENRICH,
    AVITO_PROXIES,
    AVITO_PROXY_CHANGE_URLS,
    AVITO_REQUEST_GAP_SEC,
    POLL_PERIOD_MAX_SEC,
    POLL_PERIOD_SEC,
    PRIME_ON_START,
    START_GRACE_SEC,
    START_STRICT,
    SUBSCRIPTIONS_FILE,
)
from storage import load_json, save_json

logger = logging.getLogger(__name__)


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
        self._client = AvitoHttpClient(
            proxy=self._proxy(),
            proxy_change_url=self._proxy_change_url(),
        )
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
        return await asyncio.to_thread(
            self._client.session.get,
            url,
            timeout=30,
            allow_redirects=False,
            headers={"Accept-Language": "ru,en;q=0.9", "referer": "https://www.avito.ru/"},
        )

    @staticmethod
    def _fmt_price(value: Optional[int]) -> str:
        if value is None:
            return "—"
        return f"{value:,}".replace(",", " ") + " ₽"

    async def _enrich_ad_details(self, ad: Ad):
        try:
            response = await self._avito_get(ad.url)
            if response.status_code != 200 or not response.text:
                return
            text = response.text
            soup = BeautifulSoup(text, "html.parser")

            for selector in (
                '[data-marker="seller-info/name"]',
                '[data-marker="seller-link"]',
                ".seller-info-name",
                ".seller-link",
            ):
                element = soup.select_one(selector)
                if element:
                    ad.seller_name = _get_text(element)
                    break

            seller_match = re.search(r'"ownerId"\s*:\s*"?(?P<id>\d+)"?', text)
            if seller_match:
                ad.seller_id = seller_match.group("id")

            for badge in ("Ниже рынка", "Хорошая цена", "Рыночная цена", "Выше рынка"):
                if badge in text:
                    ad.price_badge = badge
                    break

            if "рассроч" in text.lower() and "Рассрочка" not in ad.features:
                ad.features.append("Рассрочка")

            for selector in ('[data-marker="verified"]', ".verified-badge", ".is-verified"):
                if soup.select_one(selector):
                    ad.is_verified = True
                    break

            for selector in ('[data-marker="total-views"]', ".item-views", ".views-count"):
                element = soup.select_one(selector)
                if element:
                    views_match = re.search(r"\d+", _get_text(element))
                    if views_match:
                        ad.views = int(views_match.group())
                        break
            self._enrich_cache.add(ad.url)
        except Exception as exc:
            logger.warning("Ошибка обогащения объявления %s: %s", ad.url, exc)

    def _build_caption(self, ad: Ad) -> str:
        lines = [f"<b>{html.escape(ad.title)}</b>", ""]
        if ad.price is not None:
            price_line = f"💸 <b>{self._fmt_price(ad.price)}</b>"
            badges: List[str] = []
            if ad.price_badge:
                badges.append(f"✅ «{html.escape(ad.price_badge)}»")
            badges.extend(f"«{html.escape(feature)}»" for feature in ad.features)
            if badges:
                price_line += " " + " ".join(badges)
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
        lines.extend(("", avito_short_url(ad.url)))
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
            except Exception as exc:
                failed_route = self._route_key()
                logger.error("Ошибка запроса к %s: %s", self.url, exc)
                try:
                    if ADMIN_CHAT_ID is not None:
                        await self.bot.send_message(ADMIN_CHAT_ID, _br(f"Ошибка в Watcher: {exc}"))
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

    @staticmethod
    def _ad_passes_filters(ad: Ad, sub: Subscription) -> bool:
        filters = sub.flt
        text = f"{ad.title}{ad.description}".lower()
        if filters.price_min is not None and (ad.price is None or ad.price < filters.price_min):
            return False
        if filters.price_max is not None and (ad.price is None or ad.price > filters.price_max):
            return False
        if filters.keywords_all and not all(word.lower() in text for word in filters.keywords_all):
            return False
        if filters.keywords_stop and any(word.lower() in text for word in filters.keywords_stop):
            return False
        return True

    def _bump_interval(self, found_new: bool):
        self._interval = (
            max(self.interval_min, self._interval * 0.85)
            if found_new
            else min(self.interval_max, self._interval * 1.05)
        )

    def _cleanup_seen(self, ttl=7 * 24 * 3600):
        now = time.time()
        if len(self.seen) > 20000:
            oldest = sorted(self.seen.items(), key=lambda item: item[1])
            for ad_id, _ in oldest[: len(self.seen) // 2]:
                self.seen.pop(ad_id, None)
        for ad_id, seen_at in list(self.seen.items()):
            if now - seen_at > ttl:
                self.seen.pop(ad_id, None)

    async def _run(self):
        app = cast(Any, self.bot).app
        while self.has_subscribers():
            found_new = False
            ads = await self._fetch_ads()
            if ads:
                now = time.time()
                for ad in ads:
                    if ad.ad_id in self.seen:
                        continue
                    for sub in list(self.subscribers.values()):
                        if not app.license.is_active(sub.user_id):
                            logger.info("Пропуск %s для user=%s: лицензия неактивна", ad.ad_id, sub.user_id)
                            continue
                        if START_STRICT and ad.published_ts is not None and ad.published_ts + START_GRACE_SEC < sub.started_ts:
                            continue
                        if not self._ad_passes_filters(ad, sub):
                            logger.info(
                                "Пропуск %s для sub=%s: не прошёл фильтры (price=%s, title=%r)",
                                ad.ad_id,
                                sub.id,
                                ad.price,
                                ad.title,
                            )
                            continue
                        if app.sent_was_delivered(sub.user_id, ad.ad_id):
                            continue
                        if AVITO_ENRICH and ad.url not in self._enrich_cache:
                            await self._enrich_ad_details(ad)
                        chat_id = app.get_alert_chat_id(sub.user_id)
                        if chat_id and await app.send_to_alert(chat_id, self._build_caption(ad), ad.image_url):
                            if self.on_deliver:
                                self.on_deliver(sub.user_id, ad)
                            app.sent_mark(sub.user_id, ad.ad_id, now)
                            found_new = True
                            logger.info(
                                "Отправлено объявление %s пользователю %s в alert_chat=%s",
                                ad.ad_id,
                                sub.user_id,
                                chat_id,
                            )
                        else:
                            logger.warning("Не удалось отправить объявление %s: alert_chat=%s", ad.ad_id, chat_id)
                            await self._send_missing_alert_hint(app, sub.user_id)
                    self.seen[ad.ad_id] = now
            self._cleanup_seen()
            self._bump_interval(found_new)
            await asyncio.sleep(max(1.0, self._interval) * random.uniform(0.9, 1.1))

    async def _send_missing_alert_hint(self, app: Any, user_id: int) -> None:
        if not app.missing_alert_hint_once(user_id):
            return
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text="Подключить оповещения", url=app.alert_deeplink(user_id))
        ]])
        try:
            await self.bot.send_message(
                user_id,
                _br("Чтобы получать объявления, подключите бота-оповещателя:"),
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except Exception:
            pass


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
        value = self._sub_id_seq
        self._sub_id_seq += 1
        return value

    def _on_deliver(self, user_id: int, ad: Ad):
        feed = self.feed.setdefault(user_id, deque(maxlen=50))
        feed.appendleft(FeedItem(
            ts=time.time(),
            title=ad.title,
            price=ad.price,
            url=ad.url,
            date_str=ad.date_str,
        ))

    async def add_subscription(
        self,
        user_id: int,
        url: str,
        flt: Optional[SubscriberFilter] = None,
    ) -> Subscription:
        filters = flt or SubscriberFilter()
        key = search_key_from_url(url)
        sub = Subscription(
            id=self._next_sub_id(),
            user_id=user_id,
            search_key=key,
            url=url,
            flt=filters,
        )
        watcher = self.watchers.get(key)
        if not watcher:
            watcher = Watcher(key, url, self.bot, self._on_deliver)
            self.watchers[key] = watcher
            await watcher.start()
        watcher.add_sub(sub)
        self.subs_by_user.setdefault(user_id, []).append(sub)
        self.save()
        return sub

    async def remove_subscription(self, user_id: int, sub_id: int) -> bool:
        subscriptions = self.subs_by_user.get(user_id, [])
        for index, sub in enumerate(subscriptions):
            if sub.id != sub_id:
                continue
            watcher = self.watchers.get(sub.search_key)
            if watcher:
                watcher.remove_sub(sub.id)
                if not watcher.has_subscribers():
                    await watcher.stop()
                    self.watchers.pop(sub.search_key, None)
            subscriptions.pop(index)
            self.save()
            return True
        return False

    def get_sub_by_id(self, user_id: int, sub_id: int) -> Optional[Subscription]:
        return next((sub for sub in self.subs_by_user.get(user_id, []) if sub.id == sub_id), None)

    def recent_feed(self, user_id: int, limit=10) -> List[FeedItem]:
        return list(self.feed.get(user_id, deque()))[:limit]

    def save(self) -> None:
        rows = [
            {
                "id": sub.id,
                "user_id": sub.user_id,
                "search_key": sub.search_key,
                "url": sub.url,
                "name": sub.name,
                "only_new": sub.only_new,
                "started_ts": sub.started_ts,
                "filter": vars(sub.flt),
            }
            for subscriptions in self.subs_by_user.values()
            for sub in subscriptions
        ]
        save_json(self.subscriptions_file, rows)

    async def restore(self) -> None:
        try:
            rows = load_json(self.subscriptions_file, [])
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Не удалось восстановить подписки: %s", exc)
            return
        for row in rows:
            try:
                sub = Subscription(
                    id=int(row["id"]),
                    user_id=int(row["user_id"]),
                    search_key=str(row["search_key"]),
                    url=str(row["url"]),
                    flt=SubscriberFilter(**row.get("filter", {})),
                    name=row.get("name"),
                    only_new=bool(row.get("only_new", True)),
                    started_ts=float(row.get("started_ts", time.time())),
                )
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
