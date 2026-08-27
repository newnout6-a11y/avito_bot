# -*- coding: utf-8 -*-
"""
Транспортный слой Avito (версия 2026, JSON API).

Почему не HTML: Авито больше не рендерит объявления на сервере —
страница поиска отдаёт пустую JS-оболочку (проверено живыми тестами 08.2026).
Рабочий источник данных — внутренний JSON API фронтенда:
    https://www.avito.ru/web/1/js/items?categoryId=…&locationId=…&query=…&sort=date

Что здесь реализовано:
- AvitoHttpClient — curl_cffi-сессия с impersonate=chrome, прогревом cookies
  и полным набором браузерных заголовков;
- классификация блокировок Qrator: 403 JSON too-many-requests (лёгкий
  троттлинг), 429 (жёсткий IP-бан), 439 (security-challenge);
- convert_url_to_api — преобразование публичной ссылки Авито в API URL
  через бесплатный endpoint SPFA (лимит 2/мин, результат кэшируется навсегда);
- parse_api_items — разбор JSON в плоские словари под dataclass Ad бота.
"""

import hashlib
import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterator, List, Optional, Protocol
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)

AVITO_HOME = "https://www.avito.ru/"
SPFA_CONVERT_URL = "https://spfa.ru/api/avito-url/"
SPFA_MIN_INTERVAL_SEC = 31.0  # у SPFA лимит ~2 преобразования в минуту
API_ROUTE_TTL_SEC = 7 * 24 * 3600

# Базовые паузы (сек) по типу блока; дальше экспонента 2^n и джиттер.
BLOCK_BASE_WAIT = {
    "rate_limit": 30.0,   # 403 {"too-many-requests": …} — лёгкий троттлинг
    "challenge": 45.0,    # 439 «проверка безопасности»
    "ip_block": 60.0,     # 429 / 403 «Доступ ограничен: проблема с IP»
}
BLOCK_MAX_WAIT = 1800.0

BROWSER_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}

# Не-объявления в выдаче API (рекламные/сервисные блоки)
_NON_AD_TYPES = {"avatar", "banner", "vip", "personalSeller", "xl"}  # типы без urlPath отсекаются дополнительно
_CHALLENGE_MARKERS = (
    "captcha",
    "security check",
    "проверка безопасности",
    "доступ ограничен",
    "too-many-requests",
    "too many requests",
    "qrator",
)


def _retry_after_seconds(headers: Any) -> Optional[float]:
    raw = headers.get("Retry-After") if headers else None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        target = parsedate_to_datetime(str(raw))
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def is_valid_api_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.lower()
    return (
        parsed.scheme == "https"
        and (host == "avito.ru" or host.endswith(".avito.ru"))
        and (path.startswith("/web/") or path.startswith("/api/"))
    )


class AvitoBlock(Exception):
    """Блокировка со стороны Qrator/Avito."""

    def __init__(self, kind: str, status: int, retry_after: Optional[float] = None):
        super().__init__(f"avito block: {kind} (http {status})")
        self.kind = kind
        self.status = status
        self.retry_after = retry_after

    def suggested_wait(self, consecutive: int) -> float:
        base = self.retry_after if self.retry_after else BLOCK_BASE_WAIT.get(self.kind, 45.0)
        wait = min(base * (2 ** max(0, consecutive)), BLOCK_MAX_WAIT)
        return wait + random.uniform(5.0, 15.0)


class AvitoHttpError(RuntimeError):
    def __init__(self, status: int, body_preview: str = ""):
        super().__init__(f"http {status}: {body_preview}")
        self.status = status


def classify_block(status: int, headers: Any, text: str) -> Optional[AvitoBlock]:
    """Определяет тип блокировки по ответу. None — ответ не блок."""
    body = (text or "")[:200000].lower()
    location = str(headers.get("Location", "") if headers else "").lower()
    content_type = str(headers.get("Content-Type", "") if headers else "").lower()
    if status in (301, 302, 303, 307, 308):
        if any(marker in location for marker in ("captcha", "challenge", "blocked", "security")):
            return AvitoBlock("challenge", status, retry_after=_retry_after_seconds(headers))
        return None
    if status == 200:
        # Only inspect challenge markers in HTML; listing JSON may contain the same words in ad text.
        is_html = "text/html" in content_type or body.lstrip().startswith(("<!doctype", "<html"))
        if "too-many-requests" in body[:5000] or "too many requests" in body[:5000]:
            return AvitoBlock("rate_limit", status, retry_after=_retry_after_seconds(headers))
        if is_html and any(marker in body for marker in _CHALLENGE_MARKERS):
            kind = "challenge"
            return AvitoBlock(kind, status, retry_after=_retry_after_seconds(headers))
        return None
    if status == 439:
        return AvitoBlock("challenge", status)
    if status == 429:
        return AvitoBlock("ip_block", status, retry_after=_retry_after_seconds(headers))
    if status == 403:
        if text and "too-many-requests" in text[:5000]:
            return AvitoBlock("rate_limit", status)
        return AvitoBlock("ip_block", status)
    if status == 503:
        return AvitoBlock("challenge", status)
    return None


class AvitoHttpClient:
    """
    curl_cffi-сессия под Avito: impersonate=chrome, прогрев cookies,
    браузерные заголовки, один прокси со ссылкой смены IP (опционально).
    """

    def __init__(self, proxy: Optional[str] = None, proxy_change_url: Optional[str] = None,
                 timeout: float = 15.0):
        self.proxy = proxy
        self.proxy_change_url = proxy_change_url
        self.timeout = timeout
        self.warmed_at = 0.0
        self.last_status: Optional[int] = None
        self.total_ok = 0
        self.total_blocked = 0
        self._session = self._build_session()

    # ----- внутреннее -----

    def _build_session(self) -> "curl_requests.Session":
        kwargs: Dict[str, Any] = {"impersonate": "safari15_5"}
        if self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        session = curl_requests.Session(**kwargs)
        session.headers.update(BROWSER_HEADERS)
        return session

    @property
    def session(self) -> "curl_requests.Session":
        return self._session

    def warmup(self) -> bool:
        """Прогрев: заходим на главную за cookies (u, _avisc и пр.)."""
        try:
            r = self._session.get(AVITO_HOME, timeout=self.timeout, allow_redirects=False)
            self.last_status = r.status_code
            block = classify_block(r.status_code, r.headers, r.text or "")
            if block:
                self.total_blocked += 1
                raise block
            if r.status_code == 200:
                self.warmed_at = time.time()
                return True
            logger.warning("Прогрев Avito не удался: http %s", r.status_code)
        except AvitoBlock:
            raise
        except Exception as exc:
            logger.warning("Ошибка прогрева Avito: %s", exc)
        return False

    def reset(self) -> None:
        """Пересоздать сессию (после блока): чистые cookies, новый TLS."""
        try:
            self._session.close()
        except Exception:
            pass
        self._session = self._build_session()
        self.warmed_at = 0.0

    def set_proxy(self, proxy: Optional[str], change_url: Optional[str] = None) -> None:
        self.proxy = proxy
        self.proxy_change_url = change_url
        self.reset()

    def request_new_ip(self) -> bool:
        """Дёрнуть ссылку смены IP мобильного прокси (если задана)."""
        if not self.proxy_change_url:
            return False
        try:
            r = curl_requests.get(self.proxy_change_url, timeout=15)
            if 200 <= r.status_code < 300:
                logger.info("Прокси: запрошена смена IP — ок")
                return True
            logger.warning("Прокси: смена IP вернула http %s", r.status_code)
        except Exception as exc:
            logger.warning("Прокси: ошибка смены IP: %s", exc)
        return False

    # ----- основной запрос -----

    def get_search_page_items(self, search_url: str, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Прямое получение объявлений со страницы поисковой выдачи Avito (HTML-рендеринг).
        Автоматически восстанавливает сокет при сбросе соединения.
        """
        headers = {
            "referer": AVITO_HOME,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "sec-fetch-site": "none",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
        }
        for attempt in range(2):
            try:
                r = self._session.get(search_url, timeout=self.timeout, headers=headers, allow_redirects=True)
                self.last_status = r.status_code
                text = r.text or ""
                block = classify_block(r.status_code, r.headers, text)
                if block:
                    self.total_blocked += 1
                    raise block
                if r.status_code != 200:
                    raise AvitoHttpError(r.status_code, text[:200])
                self.total_ok += 1
                return parse_html_feed(text, limit=limit)
            except (AvitoBlock, AvitoHttpError):
                raise
            except Exception as exc:
                self.reset()
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                raise exc
        return []

    def get_items(self, api_url: str) -> Dict[str, Any]:
        """
        GET JSON API. Возвращает распарсенный dict.
        Бросает AvitoBlock при блокировке, RuntimeError при прочих ошибках.
        """
        if not self.warmed_at:
            self.warmup()
        headers = {
            "referer": AVITO_HOME,
            "accept": "application/json, text/plain, */*",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }
        if not is_valid_api_url(api_url):
            raise ValueError(f"invalid Avito API URL: {api_url!r}")
        for attempt in range(2):
            try:
                r = self._session.get(api_url, timeout=self.timeout, headers=headers, allow_redirects=False)
                self.last_status = r.status_code
                text = r.text or ""
                block = classify_block(r.status_code, r.headers, text)
                if block:
                    self.total_blocked += 1
                    raise block
                if r.status_code != 200:
                    raise AvitoHttpError(r.status_code, text[:200])
                self.total_ok += 1
                return json.loads(text)
            except (AvitoBlock, AvitoHttpError):
                raise
            except Exception as exc:
                self.reset()
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                raise exc
        return {}

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass


# ===== конвертация публичной ссылки -> API URL =====

_spfa_last_call = 0.0
_spfa_lock = threading.Lock()


class ApiRouteResolver(Protocol):
    def resolve(self, canonical_url: str) -> str | None: ...


class DefaultApiRouteResolver:
    """Resolve public URLs with TTL metadata and stale-route fallback."""

    def __init__(self, cache_file: str, timeout: float = 25.0):
        self.cache_file = cache_file
        self.timeout = timeout
        self.last_status = "pending"
        self.last_error: Optional[str] = None

    @staticmethod
    def _retry_delay(fail_count: int) -> float:
        return min(3600.0, 60.0 * (2 ** max(0, fail_count - 1)))

    @staticmethod
    def _as_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return default

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    def resolve(self, canonical_url: str) -> Optional[str]:
        from storage import load_state, save_state

        if is_valid_api_url(canonical_url):
            self.last_status = "ready"
            self.last_error = None
            return _ensure_sort_date(canonical_url)

        now = time.time()
        try:
            data = load_state(self.cache_file, {}) or {}
        except Exception as exc:
            data = {}
            self.last_error = str(exc)
        if not isinstance(data, dict):
            data = {}
        raw = data.get(canonical_url) if isinstance(data, dict) else None
        if isinstance(raw, str):
            raw = {"api_url": raw, "created_at": now, "last_success_at": now}
            data[canonical_url] = raw
            try:
                save_state(self.cache_file, data)
            except Exception:
                pass
        stale_route: Optional[str] = None
        if isinstance(raw, dict):
            route = raw.get("api_url")
            last = self._as_float(raw.get("last_success_at") or raw.get("created_at"))
            if route and is_valid_api_url(str(route)):
                stale_route = _ensure_sort_date(str(route))
                if now - last < API_ROUTE_TTL_SEC:
                    self.last_status = "ready"
                    self.last_error = None
                    return stale_route
            retry_after = self._as_float(raw.get("retry_after"))
            if retry_after > now:
                self.last_status = "retry"
                self.last_error = str(raw.get("last_error") or "conversion retry scheduled")
                return stale_route

        route, error = _request_api_route(canonical_url, self.timeout)
        if route:
            data[canonical_url] = {
                "api_url": route,
                "created_at": self._as_float(raw.get("created_at"), now) if isinstance(raw, dict) else now,
                "last_success_at": now,
                "last_error": None,
                "fail_count": 0,
                "retry_after": None,
            }
            self.last_status = "ready"
            self.last_error = None
        else:
            fail_count = self._as_int(raw.get("fail_count")) + 1 if isinstance(raw, dict) else 1
            self.last_status = "retry"
            self.last_error = error or "SPFA conversion failed"
            data[canonical_url] = {
                "api_url": stale_route,
                "created_at": self._as_float(raw.get("created_at"), now) if isinstance(raw, dict) else now,
                "last_success_at": self._as_float(raw.get("last_success_at")) if isinstance(raw, dict) else 0,
                "last_error": self.last_error,
                "fail_count": fail_count,
                "retry_after": now + self._retry_delay(fail_count),
            }
        try:
            save_state(self.cache_file, data)
        except Exception as exc:
            logger.warning("Не удалось сохранить metadata API route: %s", exc)
        return route or stale_route

    def invalidate(self, canonical_url: str, reason: str) -> None:
        from storage import update_state

        now = time.time()

        def mark_invalid(data: Any) -> None:
            if not isinstance(data, dict):
                return
            raw = data.get(canonical_url)
            created_at = self._as_float(raw.get("created_at"), now) if isinstance(raw, dict) else now
            fail_count = self._as_int(raw.get("fail_count")) + 1 if isinstance(raw, dict) else 1
            data[canonical_url] = {
                "api_url": None,
                "created_at": created_at,
                "last_success_at": self._as_float(raw.get("last_success_at")) if isinstance(raw, dict) else 0,
                "last_error": reason,
                "fail_count": fail_count,
                "retry_after": now + self._retry_delay(fail_count),
            }

        update_state(self.cache_file, {}, mark_invalid)
        self.last_status = "retry"
        self.last_error = reason


def invalidate_cached_api_url(url: str, cache_file: str) -> None:
    from storage import update_state

    def remove(data: Any) -> None:
        if isinstance(data, dict):
            data.pop(url, None)

    update_state(cache_file, {}, remove)


def _ensure_sort_date(api_url: str) -> str:
    """Мониторингу новых нужна сортировка по дате (на сайте это s=104)."""
    parts = urlsplit(api_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    normalized_query: list[tuple[str, str]] = []
    sort_seen = False
    for key, value in query:
        if key == "sort":
            if not sort_seen:
                normalized_query.append((key, "date"))
                sort_seen = True
            continue
        normalized_query.append((key, value))
    if not sort_seen:
        normalized_query.append(("sort", "date"))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(normalized_query), parts.fragment)
    )


def _request_api_route(url: str, timeout: float) -> tuple[Optional[str], Optional[str]]:
    global _spfa_last_call
    with _spfa_lock:
        wait = SPFA_MIN_INTERVAL_SEC - (time.monotonic() - _spfa_last_call)
        if _spfa_last_call and wait > 0:
            time.sleep(wait)
        try:
            import requests as plain_requests
            r = plain_requests.post(SPFA_CONVERT_URL, json={"url": url},
                                    headers={"Content-Type": "application/json"}, timeout=timeout)
            _spfa_last_call = time.monotonic()
            if r.status_code != 200:
                logger.warning("SPFA конвертер: http %s для %s", r.status_code, url)
                return None, f"SPFA HTTP {r.status_code}"
            payload = r.json()
            api_url = payload.get("api_url") if payload.get("success") else None
            if not api_url:
                logger.warning("SPFA конвертер не вернул api_url для %s", url)
                return None, "SPFA did not return api_url"
            api_url = _ensure_sort_date(str(api_url))
            if not is_valid_api_url(api_url):
                logger.warning("SPFA returned an invalid API URL for %s", url)
                return None, "SPFA returned invalid api_url"
            return api_url, None
        except Exception as exc:
            logger.warning("Ошибка конвертации URL %s: %s", url, exc)
            return None, str(exc)


def convert_url_to_api(url: str, cache_file: str, timeout: float = 25.0) -> Optional[str]:
    """Compatibility facade for the metadata-aware route resolver."""
    return DefaultApiRouteResolver(cache_file, timeout).resolve(url)


# ===== разбор JSON API -> плоские словари под Ad =====

def _first_str(*values: Any) -> str:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _item_image(item: Dict[str, Any]) -> str:
    gallery = item.get("gallery")
    if isinstance(gallery, dict):
        img = _first_str(gallery.get("imageLargeUrl"), gallery.get("imageUrl"))
        if img:
            return img
    images = item.get("images")
    if isinstance(images, list):
        for entry in images:
            if isinstance(entry, dict):
                for key in ("636x476", "1280x960", "1200x900", "640x480", "orig"):
                    if isinstance(entry.get(key), str):
                        return entry[key]
                for v in entry.values():
                    if isinstance(v, str) and v.startswith("http"):
                        return v
    return ""


def _item_url(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.startswith("/"):
        return f"https://www.avito.ru{raw}"
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme == "https" and (host == "avito.ru" or host.endswith(".avito.ru")):
        return raw
    return None


@dataclass
class FeedParseResult:
    items: List[Dict[str, Any]]
    warnings: List[str]
    skipped_items: int
    schema_fingerprint: str
    schema_mismatch: bool = False


def _timestamp_seconds(value: Any) -> Optional[float]:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return None
    return numeric / 1000.0 if numeric > 10_000_000_000 else numeric


def _iva_payloads(
    item: Dict[str, Any],
    step_name: str,
    component: str,
) -> List[Dict[str, Any]]:
    iva = item.get("iva")
    entries = iva.get(step_name) if isinstance(iva, dict) else None
    if not isinstance(entries, list):
        return []
    payloads: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        component_data = entry.get("componentData")
        if not isinstance(component_data, dict) or component_data.get("component") != component:
            continue
        payload = entry.get("payload")
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _item_date_str(item: Dict[str, Any]) -> str:
    for payload in _iva_payloads(item, "DateInfoStep", "date-info"):
        value = _first_str(payload.get("absolute"), payload.get("relative"))
        if value:
            return value
    return ""


def _item_description(item: Dict[str, Any]) -> str:
    direct = item.get("description")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    for payload in _iva_payloads(item, "DescriptionStep", "description"):
        value = payload.get("description")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _item_is_promoted(item: Dict[str, Any]) -> bool:
    for payload in _iva_payloads(item, "DateInfoStep", "vas"):
        vas = payload.get("vas")
        if isinstance(vas, list) and vas:
            return True
    return False


def parse_api_feed(payload: Dict[str, Any], limit: int = 20) -> FeedParseResult:
    warnings: List[str] = []
    if limit <= 0:
        return FeedParseResult([], [], 0, "empty-limit")
    if not isinstance(payload, dict):
        return FeedParseResult([], ["API payload is not an object"], 0, "invalid", True)
    catalog = payload.get("catalog")
    if not isinstance(catalog, dict):
        result = payload.get("result")
        catalog = result if isinstance(result, dict) else payload
    raw_items = catalog.get("items") if isinstance(catalog, dict) else None
    if not isinstance(raw_items, list):
        fingerprint = hashlib.sha1(repr(sorted(payload.keys())).encode()).hexdigest()[:12]
        return FeedParseResult([], ["API schema mismatch: items missing"], 0, fingerprint, True)
    ads: List[Dict[str, Any]] = []
    skipped = 0
    for item in raw_items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        if str(item.get("type", "")) in _NON_AD_TYPES:
            skipped += 1
            if "Skipped service/banner item" not in warnings:
                warnings.append("Skipped service/banner item")
            continue
        ad_id = item.get("id")
        url = _item_url(item.get("urlPath") or item.get("uriPath"))
        title = item.get("title")
        if not ad_id or not url or not title:
            skipped += 1
            if "Skipped malformed item" not in warnings:
                warnings.append("Skipped malformed item")
            continue
        price: Optional[int] = None
        pd = item.get("priceDetailed")
        candidates = [pd.get("value")] if isinstance(pd, dict) else []
        candidates.extend(item.get(k) for k in ("price", "priceValue", "priceInt"))
        for value in candidates:
            try:
                if isinstance(value, str):
                    value = value.replace(" ", "").replace("\u00a0", "")
                if isinstance(value, (int, float, str)) and float(value) > 0:
                    price = int(float(value))
                    break
            except (TypeError, ValueError):
                continue
        published_ts = _timestamp_seconds(item.get("sortTimeStamp"))
        location = ""
        for candidate in (item.get("geo"), item.get("addressDetailed"), item.get("location")):
            if isinstance(candidate, str):
                location = candidate.strip()
            elif isinstance(candidate, dict):
                location = _first_str(candidate.get("name"), candidate.get("title"), candidate.get("formattedAddress"), candidate.get("locationName"))
            if location:
                break
        ads.append({
            "ad_id": str(ad_id),
            "url": url,
            "title": str(title),
            "price": price,
            "location": location,
            "date_str": _item_date_str(item),
            "published_ts": published_ts,
            "published_exact": published_ts is not None,
            "is_promoted": _item_is_promoted(item),
            "description": _item_description(item),
            "image_url": _item_image(item),
            "seller_id": str(item["sellerId"]) if item.get("sellerId") else None,
            "is_verified": bool(item.get("isVerifiedItem")),
        })
        if len(ads) >= limit:
            break
    if raw_items and not ads:
        warnings.append("API returned zero valid listings")
    fingerprint = hashlib.sha1(repr(sorted(raw_items[0].keys()) if raw_items and isinstance(raw_items[0], dict) else []).encode()).hexdigest()[:12]
    return FeedParseResult(ads, warnings, skipped, fingerprint)


def parse_api_items(payload: Dict[str, Any], limit: int = 20) -> List[Dict[str, Any]]:
    """
    JSON API Авито -> список словарей с ключами под dataclass Ad:
    ad_id, url, title, price, location, date_str, published_ts,
    published_exact, is_promoted, description, image_url, seller_id, is_verified.
    """
    return parse_api_feed(payload, limit).items


def parse_relative_date(text: str, now: Optional[float] = None) -> Optional[float]:
    """
    Парсинг относительной/человекочитаемой даты Авито в unix timestamp (сек).
    'только что' -> now
    '15 минут назад' -> now - 15*60
    '2 часа назад' -> now - 2*3600
    'сегодня в 14:30' / 'вчера в 12:48' -> timestamp
    """
    if not text:
        return None
    if now is None:
        now = time.time()
    s = text.lower().strip()
    if any(k in s for k in ("только что", "секунд назад", "прямо сейчас")):
        return now
    m_min = re.search(r"(\d+)\s+мин", s)
    if m_min:
        return now - int(m_min.group(1)) * 60
    if "минуту назад" in s or "минуты назад" in s:
        return now - 60
    m_hr = re.search(r"(\d+)\s+час", s)
    if m_hr:
        return now - int(m_hr.group(1)) * 3600
    if "час назад" in s or "часа назад" in s:
        return now - 3600
    m_day = re.search(r"(\d+)\s+дн", s)
    if m_day:
        return now - int(m_day.group(1)) * 86400
    m_time = re.search(r"(сегодня|вчера)\s+в\s+(\d{1,2}):(\d{2})", s)
    if m_time:
        day_word, hh, mm = m_time.groups()
        dt = datetime.fromtimestamp(now)
        if day_word == "вчера":
            dt -= timedelta(days=1)
        dt = dt.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        return dt.timestamp()
    return None


def _find_catalog(node: Any, _depth: int = 0) -> Optional[Dict[str, Any]]:
    """Recursively locate a catalog-like dict that holds an items list of ads."""
    if _depth > 12:
        return None
    if isinstance(node, dict):
        items = node.get("items")
        if isinstance(items, list) and any(
            isinstance(entry, dict)
            and any(key in entry for key in ("urlPath", "uriPath", "sortTimeStamp", "id"))
            for entry in items
        ):
            return node
        for value in node.values():
            found = _find_catalog(value, _depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_catalog(value, _depth + 1)
            if found is not None:
                return found
    return None


def _iter_brace_blocks(text: str) -> Iterator[str]:
    """Yield top-level balanced {...} substrings, ignoring braces inside strings."""
    depth = 0
    start = -1
    in_str = False
    escape = False
    quote = ""
    for index, char in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                in_str = False
            continue
        if char in ('"', "'"):
            in_str = True
            quote = char
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                yield text[start : index + 1]
                start = -1


def _script_state_candidates(body: str) -> Iterator[Any]:
    """Yield parsed JSON states from a <script> body across Avito's embedding forms."""
    text = body.strip()
    if not text:
        return
    # 1) Whole script is JSON (e.g. <script type="application/json">).
    try:
        yield json.loads(text)
        return
    except (ValueError, TypeError):
        pass
    # 2) URI-encoded JSON string literal, e.g. window.__initialData__ = "%7B...%7D".
    for match in re.finditer(r'"((?:[^"\\]|\\.)*)"', text):
        raw = match.group(1)
        if "%7b" not in raw.lower():
            continue
        try:
            yield json.loads(unquote(raw))
        except (ValueError, TypeError):
            continue
    # 3) Direct object assignment, e.g. window.__initialData__ = {...};
    for block in _iter_brace_blocks(text):
        try:
            yield json.loads(block)
        except (ValueError, TypeError):
            continue


def _embedded_catalog(soup: BeautifulSoup) -> Optional[Dict[str, Any]]:
    for script in soup.find_all("script"):
        body = script.string or script.get_text() or ""
        if "catalog" not in body or "sortTimeStamp" not in body:
            continue
        for state in _script_state_candidates(body):
            catalog = _find_catalog(state)
            if catalog is not None:
                return catalog
    return None


def parse_html_feed(html_text: str, limit: int = 50) -> List[Dict[str, Any]]:
    """
    Парсинг карточек объявлений напрямую из HTML страницы поисковой выдачи Avito.
    """
    if not html_text:
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    catalog = _embedded_catalog(soup)
    if catalog is not None:
        parsed = parse_api_feed({"catalog": catalog}, limit=limit)
        if parsed.items:
            return parsed.items

    items_elements = soup.select('[data-marker="item"]')
    if not items_elements:
        items_elements = soup.select('div[itemprop="itemListElement"]')
    ads: List[Dict[str, Any]] = []
    for item in items_elements[:limit]:
        item_id = item.get("data-item-id") or item.get("id")
        title_el = item.select_one('[itemprop="name"], [data-marker="item-title"]')
        title = title_el.get_text(strip=True) if title_el else ""
        if not item_id or not title:
            continue
        link_el = item.select_one('a[itemprop="url"], a[data-marker="item-title"]')
        href = link_el.get("href") if link_el else ""
        url = ("https://www.avito.ru" + href) if href.startswith("/") else href
        if not url:
            continue
        price: Optional[int] = None
        price_meta = item.select_one('meta[itemprop="price"]')
        if price_meta and price_meta.get("content", "").isdigit():
            price = int(price_meta.get("content"))
        else:
            price_el = item.select_one('[data-marker="item-price"], [itemprop="price"]')
            if price_el:
                digits = re.sub(r"[^\d]", "", price_el.get_text(strip=True))
                if digits.isdigit():
                    price = int(digits)
        img_el = item.select_one('img[itemprop="image"], img[class*="image"]')
        image_url = (img_el.get("src") or img_el.get("data-src")) if img_el else None
        desc_el = item.select_one('[class*="description"], [data-marker="item-description"]')
        description = desc_el.get_text(strip=True) if desc_el else ""
        date_el = item.select_one('[data-marker="item-date"]')
        date_str = date_el.get_text(strip=True) if date_el else ""
        published_ts = parse_relative_date(date_str)
        loc_el = item.select_one('[class*="geo"], [data-marker="item-address"]')
        location = loc_el.get_text(strip=True) if loc_el else ""
        ads.append({
            "ad_id": str(item_id),
            "url": url,
            "title": str(title),
            "price": price,
            "location": location,
            "date_str": date_str,
            "published_ts": published_ts,
            "published_exact": False,
            "is_promoted": False,
            "description": description,
            "image_url": image_url,
            "seller_id": None,
            "is_verified": False,
        })
    return ads
