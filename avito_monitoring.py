"""Polling workers and subscription persistence for the Avito monitor."""

import asyncio
import html
import logging
import os
import random
import re
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, cast

from aiogram import Bot, types
from bs4 import BeautifulSoup

import avito_sitemap
from avito_api import (
    ApiRouteResolver,
    AvitoBlock,
    AvitoHttpClient,
    AvitoHttpError,
    DefaultApiRouteResolver,
    invalidate_cached_api_url,
    parse_api_feed,
)
from avito_domain import (
    Ad,
    FeedItem,
    SearchSpec,
    SubscriberFilter,
    Subscription,
    _br,
    _fmt_dt,
    _get_text,
    avito_short_url,
    keyword_in_text,
    normalize_filter,
    parse_avito_url,
    translit_to_cyrillic,
)
from avito_settings import (
    ADMIN_CHAT_ID,
    API_URLS_FILE,
    AVITO_ENRICH,
    AVITO_PROXIES,
    AVITO_PROXY_CHANGE_URLS,
    AVITO_REQUEST_GAP_SEC,
    ONLY_NEW_ID_GATE,
    POLL_PERIOD_MAX_SEC,
    POLL_PERIOD_SEC,
    PRIME_ON_START,
    START_GRACE_SEC,
    START_STRICT,
    SUBSCRIPTIONS_FILE,
)
from storage import load_state, save_state

logger = logging.getLogger(__name__)

# Parole-режим после ip_block: первые N опросов реже в PAROLE_FACTOR раз.
# Живой замер 08.2026: сразу после разбана 4 запроса за 90 сек ре-банят.
PAROLE_POLLS = 3
PAROLE_FACTOR = 4.0


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
        route_resolver: Optional[ApiRouteResolver] = None,
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
            cookie_store=self._cookie_store_path(),
        )
        self._client_closed = False
        self.on_deliver = on_deliver
        self.route_resolver = route_resolver or DefaultApiRouteResolver(API_URLS_FILE)
        self._enrich_cache: set[str] = set()
        self._api_url: Optional[str] = None
        self._api_route_managed = False
        self._skip_initial_poll = False
        self._primed = False
        self._license_skip_logged = False
        self._wake = asyncio.Event()
        self._sleep_task: Optional[asyncio.Task] = None
        self._id_frontier = 0
        self._prev_batch_max_id = 0
        self._window_cap = 0.0
        self._blocked_until = 0.0
        self._consecutive_blocks = 0
        self._parole_polls_left = 0
        self.last_http_status: Optional[int] = None
        self.last_block_kind: Optional[str] = None
        self.conversion_status = "pending"
        self.conversion_error: Optional[str] = None
        self.parser_health = "unknown"
        self.parser_warnings: tuple[str, ...] = ()

    async def start(self):
        if self.task and not self.task.done():
            return
        if self._client_closed:
            self._client = AvitoHttpClient(
                proxy=self._proxy(),
                proxy_change_url=self._proxy_change_url(),
                cookie_store=self._cookie_store_path(),
            )
            self._client_closed = False
        # Восстановленная подписка может принадлежать пользователю с уже
        # истёкшей лицензией (WatcherManager.restore — при старте бота их
        # может быть много). Прайминг — реальный запрос к Avito впустую,
        # раз доставлять всё равно некому; отложить до первого подписчика
        # с активной лицензией (см. _run) вместо запроса на каждый рестарт.
        app = cast(Any, self.bot).app
        if PRIME_ON_START and self._has_active_subscriber(app):
            await self._prime_seen(None)
        self.task = asyncio.create_task(self._run(), name=f"watch:{self.search_key}")

    def _has_active_subscriber(self, app: Any) -> bool:
        return any(app.license.is_active(sub.user_id) for sub in self.subscribers.values())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        self._client.close()
        self._client_closed = True

    def has_subscribers(self):
        return bool(self.subscribers)

    @property
    def consecutive_blocks(self) -> int:
        return self._consecutive_blocks

    def cooldown_remaining(self) -> float:
        """Секунд до конца паузы защиты от блокировок (0 — не в паузе).

        Публичный доступ для UI (get_watcher_status/diag), чтобы avito_ui не
        лез в приватные _blocked_until / _route_key / _route_blocked_until.
        """
        route_until = Watcher._route_blocked_until.get(self._route_key(), 0.0)
        return max(0.0, max(self._blocked_until, route_until) - time.monotonic())

    def add_sub(self, sub: Subscription):
        self.subscribers[sub.id] = sub

    def remove_sub(self, sub_id: int):
        self.subscribers.pop(sub_id, None)

    def _proxy(self) -> Optional[str]:
        return AVITO_PROXIES[self._proxy_index] if AVITO_PROXIES else None

    def _cookie_store_path(self) -> str:
        """Cookie store per route (proxy): одна «личность» на канал выхода."""
        route = self._route_key()
        slug = re.sub(r"[^a-z0-9]+", "_", route.lower())[:48] or "direct"
        data_dir = os.getenv("DATABASE_FILE", os.path.join("data", "avito_monitor.sqlite3"))
        return os.path.join(os.path.dirname(data_dir) or "data", f"cookies_{slug}.json")

    async def _mark_seen_from_sitemap(self) -> int:
        """Sitemap-fallback при ip_block: пометить свежие item-ID в seen."""
        slug, cat_id = self._sitemap_category()
        if not slug and not cat_id:
            return 0
        try:
            return await asyncio.to_thread(
                avito_sitemap.mark_seen_from_sitemap, slug, cat_id, self.seen
            )
        except Exception as exc:
            logger.warning("Sitemap seen-marking не удался: %s", exc)
            return 0

    def _sitemap_category(self) -> tuple[Optional[str], Optional[int]]:
        """(category_slug, category_id) для текущего поиска, если известны."""
        url = self._api_url or ""
        m = re.search(r"[?&]categoryId=(\d+)", url)
        cat_id = int(m.group(1)) if m else None
        if not cat_id:
            return None, None
        # slug — второй сегмент пути публичного URL: avito.ru/<city>/<slug>/...
        try:
            path = self.url.split("?", 1)[0].split("#", 1)[0]
            parts = [p for p in path.split("/")[3:] if p]
            slug = parts[1] if len(parts) >= 2 else None
        except Exception:
            slug = None
        if slug and re.fullmatch(r"[a-z0-9_]+", slug):
            return slug, cat_id
        return None, cat_id

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
    def _numeric_id(ad_id: str) -> int:
        """item-ID Avito как int (0 если не число — тогда id-калитка не применяется)."""
        try:
            return int(str(ad_id).strip())
        except (TypeError, ValueError):
            return 0

    def _note_id_frontier(self, ads: List[Ad]) -> None:
        """Поднять фронтир свежести до максимального item-ID в партии."""
        batch_max = max((self._numeric_id(ad.ad_id) for ad in ads), default=0)
        if batch_max > self._id_frontier:
            self._id_frontier = batch_max

    @staticmethod
    def _feed_window_seconds(ads: List[Ad]) -> float:
        """Сколько секунд «покрывает» текущая страница выдачи (span по времени)."""
        stamps = [ad.published_ts for ad in ads if ad.published_ts]
        return (max(stamps) - min(stamps)) if len(stamps) >= 2 else 0.0

    def _watch_feed_window(self, ads: List[Ad], names_str: str) -> None:
        """Держать темп быстрее прокрутки ленты и ловить пропуски по item-ID.

        Выдача показывает ~50 объявлений. Если между опросами их «утекло»
        больше, чем помещается на странице, новые ушли из окна незамеченными —
        это видно по монотонным ID: min(текущей партии) > max(прошлой) значит
        окна не пересеклись, целая пачка проскочила. Тогда ускоряемся и кричим.
        """
        batch_ids = [i for i in (self._numeric_id(ad.ad_id) for ad in ads) if i]
        batch_min = min(batch_ids, default=0)
        if self._prev_batch_max_id and batch_min and batch_min > self._prev_batch_max_id:
            logger.warning(
                "[Мониторинг] %s: лента прокрутилась целиком между опросами "
                "(min id %s > прошлый max %s) — вероятен пропуск новых. "
                "Ускоряю опрос; если повторяется — уменьшите POLL_PERIOD_SEC "
                "или категория слишком объёмная для одной страницы.",
                names_str, batch_min, self._prev_batch_max_id,
            )
            self._interval = self.interval_min
        if batch_ids:
            self._prev_batch_max_id = max(batch_ids)
        span = self._feed_window_seconds(ads)
        if span:
            # перепроверять до того, как утечёт половина видимого окна
            self._window_cap = max(self.interval_min, span * 0.5)

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
        if ad.published_exact and ad.published_ts:
            lines.append("🗓 " + _fmt_dt(ad.published_ts))
        elif ad.date_str:
            lines.append("🗓 " + html.escape(ad.date_str))
        elif ad.published_ts:
            lines.append("🗓 " + _fmt_dt(ad.published_ts))
        if ad.is_promoted:
            lines.append("📣 Продвинуто")
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

    async def _invalidate_api_route(self, reason: str) -> None:
        invalidate = getattr(self.route_resolver, "invalidate", None)
        if callable(invalidate):
            await asyncio.to_thread(invalidate, self.url, reason)
            return
        await asyncio.to_thread(invalidate_cached_api_url, self.url, API_URLS_FILE)

    async def _fetch_api_fallback(self) -> Optional[List[Ad]]:
        """Use the JSON endpoint only when the primary HTML request failed."""
        if not self._api_url:
            self.conversion_status = "pending"
            self.conversion_error = None
            try:
                route = await asyncio.to_thread(self.route_resolver.resolve, self.url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.conversion_status = "retry"
                self.conversion_error = str(exc)
                logger.warning(
                    "[Мониторинг] Не удалось получить резервный API-маршрут для %s: %s",
                    self.search_key[:40],
                    exc,
                )
                return None
            if not route:
                self.conversion_status = "retry"
                self.conversion_error = "API-маршрут не найден"
                return None
            self._api_url = route
            self._api_route_managed = True

        try:
            payload = await asyncio.to_thread(self._client.get_items, self._api_url)
        except asyncio.CancelledError:
            raise
        except AvitoBlock as block:
            # Троттлинг/бан по IP — временное состояние запроса, не признак
            # того, что маршрут (URL) сам по себе стал невалидным. Инвалидация
            # тут стирала бы рабочий api_url на весь ip_block backoff (до часа)
            # и роняла резервный путь в SPFA (31с, 2/мин) вместо простого ожидания.
            self.last_http_status = block.status
            self.last_block_kind = block.kind
            self.conversion_status = "retry"
            self.conversion_error = str(block)
            raise
        except Exception as api_error:
            self.last_http_status = getattr(api_error, "status", self._client.last_status)
            self.last_block_kind = getattr(api_error, "kind", None)
            self.conversion_status = "retry"
            self.conversion_error = str(api_error)
            # Инвалидировать маршрут только когда сам URL подтверждённо плох
            # (сервер прямо сказал "нет такого"), а не на любую ошибку сети.
            is_route_invalid = isinstance(api_error, AvitoHttpError) and api_error.status in (
                400, 404, 410, 422,
            )
            if self._api_route_managed and is_route_invalid:
                try:
                    await self._invalidate_api_route(str(api_error))
                except Exception as cache_error:
                    logger.warning("Could not invalidate API route: %s", cache_error)
                self._api_url = None
                self._api_route_managed = False
            raise

        parsed = parse_api_feed(payload, limit=50)
        self.parser_warnings = tuple(parsed.warnings)
        if parsed.schema_mismatch:
            self.parser_health = "schema_mismatch"
            self.conversion_status = "retry"
            self.conversion_error = "; ".join(parsed.warnings)
            await self._invalidate_api_route(self.conversion_error)
            self._api_url = None
            self._api_route_managed = False
            return None
        self.last_http_status = self._client.last_status
        if not parsed.items:
            # Валидный ответ, схема цела, но пусто. Это либо реально нулевая
            # выдача, либо известная «мягкая» отдача Avito сразу после PoW
            # (research/probe_zero_items.py). Маршрут не трогаем, но и статус
            # не оставляем протухшим со старого удачного цикла.
            self.parser_health = "empty" if not parsed.warnings else "warning"
            self.conversion_status = "ready"
            self.conversion_error = "; ".join(parsed.warnings) or None
            return None
        self.parser_health = "warning" if parsed.warnings else "ok"
        self.conversion_status = "ready"
        self.conversion_error = None
        return [Ad(**item) for item in parsed.items]

    async def _fetch_ads(self) -> Optional[List[Ad]]:
        """Fetch the feed with per-watcher and shared-route cooldowns."""
        now = time.monotonic()
        route_key = self._route_key()
        route_blocked_until = Watcher._route_blocked_until.get(route_key, 0.0)
        if route_blocked_until and route_blocked_until <= now:
            Watcher._route_blocked_until.pop(route_key, None)
            route_blocked_until = 0.0
        if now < max(self._blocked_until, route_blocked_until):
            return None

        max_attempts = max(1, len(AVITO_PROXIES))
        if self._proxy_change_url():
            max_attempts = max(max_attempts, 2)
        for attempt in range(max_attempts):
            try:
                primary_error: Optional[Exception] = None
                await self._wait_global_rate_limit()
                try:
                    raw_items = await asyncio.to_thread(
                        self._client.get_search_page_items,
                        self.url,
                        50,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    primary_error = exc
                    raw_items = []
                if raw_items:
                    self.last_http_status = self._client.last_status
                    self._consecutive_blocks = 0
                    self.last_block_kind = None
                    self.parser_health = "ok"
                    self.parser_warnings = ()
                    self.conversion_status = "ready"
                    self.conversion_error = None
                    return [Ad(**item) for item in raw_items]

                # Если HTML-путь уже упёрся в бан/троттлинг по IP — не отправлять
                # второй запрос за JSON на том же IP. Тот же принцип, что и в
                # 20-минутном backoff для ip_block: лишний стук продлевает бан.
                # 439 (challenge) — исключение: он решаемый, и cookie pow_challenge
                # после HTML-попытки может пропустить API-запрос.
                if isinstance(primary_error, AvitoBlock) and primary_error.kind in (
                    "ip_block",
                    "rate_limit",
                ):
                    raise primary_error

                await self._wait_global_rate_limit()
                try:
                    fallback_items = await self._fetch_api_fallback()
                except asyncio.CancelledError:
                    raise
                except Exception as fallback_error:
                    if primary_error is None:
                        primary_error = fallback_error
                else:
                    if fallback_items:
                        self._consecutive_blocks = 0
                        self.last_block_kind = None
                        return fallback_items

                if primary_error is not None:
                    raise primary_error
                return None
            except AvitoBlock as block:
                blocked_route = self._route_key()
                self.last_http_status = block.status
                self.last_block_kind = block.kind
                wait = block.suggested_wait(self._consecutive_blocks)
                self._consecutive_blocks = min(self._consecutive_blocks + 1, 5)
                self._blocked_until = time.monotonic() + wait
                Watcher._route_blocked_until[blocked_route] = self._blocked_until
                if block.kind == "ip_block":
                    # Sitemap открыт даже при IP-бане (CDN вне Qrator):
                    # помечаем свежие item-ID в seen, чтобы после разбана
                    # не засыпать пользователя накопившимся старьём.
                    await self._mark_seen_from_sitemap()
                    # После снятия бана IP на «проверочном режиме» (живой
                    # замер: 4 запроса за 90 сек ре-банят) — parole-режим.
                    self._parole_polls_left = PAROLE_POLLS
                route_changed = self._rotate_proxy()
                if route_changed and attempt + 1 < max_attempts:
                    self._blocked_until = 0
                    continue
                return None
            except AvitoHttpError as error:
                self.last_http_status = error.status
                if error.status in (400, 404, 410, 422) and self._api_url:
                    try:
                        await self._invalidate_api_route(str(error))
                    except Exception as cache_error:
                        logger.warning("Could not invalidate API URL cache: %s", cache_error)
                    self._api_url = None
                    self.conversion_status = "retry"
                    self.conversion_error = str(error)
                logger.error("Avito HTTP error for %s: %s", self.url, error)
                self._blocked_until = time.monotonic() + min(60.0, self.interval_min)
                Watcher._route_blocked_until[self._route_key()] = self._blocked_until
                return None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed_route = self._route_key()
                self._client.reset()
                err_str = str(exc)
                logger.error("Ошибка запроса к %s: %s", self.url, exc)
                is_transient_net = any(k in err_str for k in ("(56)", "(28)", "(35)", "(7)", "Connection closed", "timed out", "reset by peer"))
                if not is_transient_net:
                    try:
                        if ADMIN_CHAT_ID is not None:
                            await self.bot.send_message(ADMIN_CHAT_ID, _br(f"Ошибка в Watcher: {exc}"))
                    except Exception:
                        pass
                route_changed = self._rotate_proxy()
                if route_changed and attempt + 1 < max_attempts:
                    await asyncio.sleep(2 + random.random() * 2)
                    continue
                self._blocked_until = time.monotonic() + min(30.0, self.interval_min)
                Watcher._route_blocked_until[failed_route] = self._blocked_until
        return None

    async def _prime_seen(self, limit: Optional[int] = None):
        sub_names = [s.name or f"Поиск #{s.id}" for s in self.subscribers.values()]
        name_hint = ", ".join(f"«{n}»" for n in sub_names) if sub_names else self.search_key[:40]
        logger.info("[Мониторинг] Инициализация %s: загрузка текущих объявлений с Avito...", name_hint)
        ads = await self._fetch_ads()
        if not ads:
            logger.warning("[Мониторинг] Инициализация %s: не удалось получить первичные объявления", name_hint)
            return
        to_prime = ads[:limit] if limit is not None else ads
        for ad in to_prime:
            self.seen[ad.ad_id] = time.time()
        self._note_id_frontier(ads)
        self._watch_feed_window(ads, name_hint)
        self._skip_initial_poll = True
        self._primed = True
        logger.info("[Мониторинг] Инициализация %s завершена: запомнено %d объявлений, запущен мониторинг новых", name_hint, len(self.seen))

    @staticmethod
    def _ad_passes_filters(ad: Ad, sub: Subscription) -> bool:
        filters = sub.flt
        if filters.price_min is not None and (ad.price is None or ad.price < filters.price_min):
            return False
        if filters.price_max is not None and (ad.price is None or ad.price > filters.price_max):
            return False
        raw = f"{ad.title} {ad.description}"
        text_cf = raw.casefold()
        text_tr = translit_to_cyrillic(raw)

        def has(word: str) -> bool:
            return keyword_in_text(word, text_cf, text_tr)

        if filters.keywords_all and not all(has(w) for w in filters.keywords_all):
            return False
        if filters.keywords_any and not any(has(w) for w in filters.keywords_any):
            return False
        if filters.keywords_stop and any(has(w) for w in filters.keywords_stop):
            return False
        return True

    def _bump_interval(self, found_new: bool):
        self._interval = (
            max(self.interval_min, self._interval * 0.85)
            if found_new
            else min(self.interval_max, self._interval * 1.05)
        )
        # Не давать адаптивному дрейфу подниматься выше окна ленты: иначе в
        # объёмной категории опрос замедляется до 300с, а окно ~6мин, и новое
        # утекает. Границу interval_min (порог анти-бана) не переступаем.
        if self._window_cap:
            self._interval = min(self._interval, max(self.interval_min, self._window_cap))

    def _poll_delay(self) -> float:
        """Пауза между опросами; после ip_block — parole-режим (реже в N раз).

        Живой замер 08.2026: сразу после снятия бана IP на «проверочном
        режиме» — 4 запроса за 90 сек ре-банят. Первые запросы после бана
        должны идти с большим интервалом.
        """
        delay = max(1.0, self._interval) * random.uniform(0.9, 1.1)
        if self._parole_polls_left > 0:
            self._parole_polls_left -= 1
            delay *= PAROLE_FACTOR
            logger.info(
                "[Мониторинг] %s: parole-режим после бана, пауза ~%dс (осталось %d)",
                self.search_key[:32], int(delay), self._parole_polls_left,
            )
        return delay

    def request_poll_now(self) -> None:
        """Разбудить цикл опроса немедленно («Обновить сейчас»), не переинициализируя.

        Прайминг (Watcher.start) помечает всю текущую выдачу как виденную —
        то есть глушит ровно те объявления, ради которых пользователь и жмёт
        кнопку. Здесь мы только просим цикл проснуться раньше срока.
        """
        self._wake.set()
        sleep_task = self._sleep_task
        if sleep_task is not None and not sleep_task.done():
            sleep_task.cancel()

    async def _sleep_or_wake(self, delay: float) -> None:
        """Спать `delay` секунд, но проснуться раньше по request_poll_now().

        Идёт через asyncio.sleep (а не Event.wait), чтобы отмена/патч сна
        работали как раньше. request_poll_now() отменяет именно этот сон и
        ставит флаг — отличаем «разбудили» от stop()/реальной отмены по флагу.
        """
        if self._wake.is_set():  # poke пришёл, пока шёл сетевой запрос
            self._wake.clear()
            return
        sleep_task = asyncio.ensure_future(asyncio.sleep(max(0.0, delay)))
        self._sleep_task = sleep_task
        try:
            await sleep_task
        except asyncio.CancelledError:
            if self._wake.is_set():
                self._wake.clear()
                return
            raise
        finally:
            self._sleep_task = None
            if not sleep_task.done():
                sleep_task.cancel()

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
        sub_names = [s.name or f"Поиск #{s.id}" for s in self.subscribers.values()]
        name_hint = ", ".join(f"«{n}»" for n in sub_names) if sub_names else self.search_key[:40]
        logger.info("[Мониторинг] Фоновый процесс активен для %s (интервал: ~%d сек)", name_hint, int(self._interval))
        if self._skip_initial_poll:
            self._skip_initial_poll = False
            await self._sleep_or_wake(max(1.0, self._interval) * random.uniform(0.9, 1.1))
        while self.has_subscribers():
            found_new = False
            sub_names = [s.name or f"Поиск #{s.id}" for s in self.subscribers.values()]
            names_str = ", ".join(f"«{n}»" for n in sub_names) if sub_names else self.search_key[:40]
            if not self._has_active_subscriber(app):
                # Ни у кого из подписчиков нет активного ключа — доставлять
                # всё равно некому. Раньше вотчер продолжал долбить Avito на
                # полном темпе ради ads, которые тут же отбрасывались в цикле
                # доставки: тратили бюджет запросов (и риск бана) в никуда.
                if not self._license_skip_logged:
                    logger.info(
                        "[Мониторинг] %s: ни у одного подписчика нет активной лицензии — "
                        "опрос Avito приостановлен без сетевых запросов (проверка раз в ~%dс)",
                        names_str, int(self.interval_max),
                    )
                    self._license_skip_logged = True
                self._interval = self.interval_max
                await self._sleep_or_wake(self._poll_delay())
                continue
            self._license_skip_logged = False
            if PRIME_ON_START and not self._primed:
                # start() пропустил прайминг (тогда активных не было) — теперь
                # кто-то активировался. Один тихий запрос на baseline seen,
                # без доставки, чтобы не вывалить на него весь текущий фид
                # пачкой «новых» — так же, как обычный прайминг на старте.
                await self._prime_seen(None)
                self._bump_interval(found_new=False)
                await self._sleep_or_wake(self._poll_delay())
                continue
            ads = await self._fetch_ads()
            if ads is None:
                route_until = Watcher._route_blocked_until.get(self._route_key(), 0.0)
                cooldown = max(self._blocked_until, route_until) - time.monotonic()
                if cooldown > 0:
                    logger.info(
                        "[Мониторинг] %s: пауза защиты от блокировок (ещё ~%dс до следующего опроса)",
                        names_str, int(cooldown),
                    )
                else:
                    # None без активного cooldown = запрос не дал данных и не
                    # выставил блок (пустой валидный ответ, сетевой сбой на
                    # ретраях и т.п.). Не называть это «защитой от блокировок».
                    logger.info(
                        "[Мониторинг] %s: опрос без данных (%s), повтор через ~%dс",
                        names_str, self.last_block_kind or self.parser_health or "нет ответа",
                        int(self._interval),
                    )
            elif not ads:
                logger.info("[Мониторинг] %s: получено 0 объявлений от Avito API", names_str)
            else:
                new_count = 0
                now = time.time()
                self._watch_feed_window(ads, names_str)
                # Снимок состояния на весь цикл, а не полное чтение на каждую
                # пару (ad, sub). sent_mark ниже всё равно пишет сразу, снимок
                # держим согласованным вручную.
                sent_snapshot = app.dedup_snapshot()
                bindings_snapshot = app.bindings_snapshot()
                for ad in ads:
                    was_seen = ad.ad_id in self.seen
                    for sub in list(self.subscribers.values()):
                        if was_seen and sub.only_new:
                            continue
                        if not app.license.is_active(sub.user_id):
                            logger.info("Пропуск %s для user=%s: лицензия неактивна", ad.ad_id, sub.user_id)
                            continue
                        if (
                            sub.only_new
                            and ONLY_NEW_ID_GATE
                            and self._id_frontier
                            and 0 < self._numeric_id(ad.ad_id) <= self._id_frontier
                        ):
                            logger.info(
                                "Пропуск %s (%s) для sub=%s: item-ID %s ≤ фронтира свежести %s "
                                "— поднятое/перепубликованное старое",
                                ad.ad_id, ad.title, sub.id, ad.ad_id, self._id_frontier,
                            )
                            continue
                        if sub.only_new and START_STRICT and ad.published_ts is None:
                            logger.info(
                                "Пропуск %s (%s) для sub=%s: нет времени публикации",
                                ad.ad_id,
                                ad.title,
                                sub.id,
                            )
                            continue
                        # Точное время (sortTimeStamp из каталога) — узкий grace.
                        # Относительная дата из DOM-фолбэка («N минут/часов назад»)
                        # огрублена до часа — иначе эта ветка вообще не может ничего
                        # доставить при дефолтах. От поднятого старья тут страхует
                        # калитка по item-ID выше, а не флаг published_exact.
                        freshness_grace = (
                            START_GRACE_SEC if ad.published_exact
                            else max(START_GRACE_SEC, 3600)
                        )
                        if (
                            sub.only_new
                            and START_STRICT
                            and ad.published_ts is not None
                            and ad.published_ts + freshness_grace < sub.started_ts
                        ):
                            logger.info(
                                "Пропуск %s (%s) для sub=%s: старое%s объявление "
                                "(дата: %s, поиск запущен: %s)",
                                ad.ad_id,
                                ad.title,
                                sub.id,
                                " продвинутое" if ad.is_promoted else "",
                                ad.date_str or _fmt_dt(ad.published_ts),
                                _fmt_dt(sub.started_ts),
                            )
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
                        if ad.ad_id in sent_snapshot.get(str(sub.user_id), {}):
                            continue
                        if AVITO_ENRICH and ad.url not in self._enrich_cache:
                            await self._enrich_ad_details(ad)
                        raw_chat_id = bindings_snapshot.get(str(sub.user_id))
                        chat_id = int(raw_chat_id) if raw_chat_id else None
                        if chat_id and await app.send_to_alert(
                            chat_id, self._build_caption(ad), ad.image_url, item_url=ad.url
                        ):
                            if self.on_deliver:
                                self.on_deliver(sub.user_id, ad)
                            app.sent_mark(sub.user_id, ad.ad_id, now)
                            sent_snapshot.setdefault(str(sub.user_id), {})[ad.ad_id] = now
                            found_new = True
                            new_count += 1
                            logger.info(
                                "Отправлено объявление %s пользователю %s в alert_chat=%s (%s)",
                                ad.ad_id,
                                sub.user_id,
                                chat_id,
                                ad.title,
                            )
                        else:
                            logger.warning("Не удалось отправить объявление %s: alert_chat=%s", ad.ad_id, chat_id)
                            await self._send_missing_alert_hint(app, sub.user_id)
                    self.seen[ad.ad_id] = now
                self._note_id_frontier(ads)
                if new_count == 0:
                    logger.info("[Мониторинг] %s: получено %d объявлений, новых нет. След. проверка через ~%dс", names_str, len(ads), int(self._interval))
                else:
                    logger.info("[Мониторинг] %s: найдено и отправлено новых объявлений: %d! След. проверка через ~%dс", names_str, new_count, int(self._interval))
            self._cleanup_seen()
            self._bump_interval(found_new)
            await self._sleep_or_wake(self._poll_delay())

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
        return await self.add_search_spec(user_id, parse_avito_url(url), flt)

    async def add_search_spec(
        self,
        user_id: int,
        spec: SearchSpec,
        flt: Optional[SubscriberFilter] = None,
    ) -> Subscription:
        if spec.kind != "search":
            raise ValueError("Для подписки нужна ссылка на результаты поиска")
        filters = normalize_filter(flt or spec.filters)
        key = spec.search_key
        sub = Subscription(
            id=self._next_sub_id(),
            user_id=user_id,
            search_key=key,
            url=key,
            flt=filters,
            original_url=spec.display_url,
        )
        watcher = self.watchers.get(key)
        if not watcher:
            watcher = Watcher(key, spec.canonical_url, self.bot, self._on_deliver)
            self.watchers[key] = watcher
        watcher.add_sub(sub)
        await watcher.start()
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
                "original_url": sub.original_url,
                "name": sub.name,
                "only_new": sub.only_new,
                "started_ts": sub.started_ts,
                "filter": vars(sub.flt),
            }
            for subscriptions in self.subs_by_user.values()
            for sub in subscriptions
        ]
        save_state(self.subscriptions_file, rows)

    async def restore(self) -> None:
        try:
            rows = load_state(self.subscriptions_file, [])
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Не удалось восстановить подписки: %s", exc)
            return
        for row in rows:
            try:
                candidates = (
                    row.get("url"),
                    row.get("search_key"),
                    row.get("original_url"),
                )
                spec = None
                for candidate in candidates:
                    if not candidate:
                        continue
                    try:
                        spec = parse_avito_url(str(candidate))
                        break
                    except (TypeError, ValueError):
                        continue
                if spec is None or spec.kind != "search":
                    raise ValueError("нет восстанавливаемой поисковой ссылки")
                sub = Subscription(
                    id=int(row["id"]),
                    user_id=int(row["user_id"]),
                    search_key=spec.search_key,
                    url=spec.canonical_url,
                    flt=normalize_filter(SubscriberFilter(**row.get("filter", {}))),
                    original_url=row.get("original_url") or spec.display_url,
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
        total_subs = sum(len(s) for s in self.subs_by_user.values())
        logger.info("[Мониторинг] Восстановлено подписок: %d, активных поисков: %d", total_subs, len(self.watchers))
        for watcher in self.watchers.values():
            await watcher.start()

    async def close(self) -> None:
        """Stop all polling workers and release their HTTP sessions."""
        watchers = list(self.watchers.values())
        self.watchers.clear()
        for watcher in watchers:
            await watcher.stop()
