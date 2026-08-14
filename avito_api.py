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

import json
import logging
import random
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)

AVITO_HOME = "https://www.avito.ru/"
SPFA_CONVERT_URL = "https://spfa.ru/api/avito-url/"
SPFA_MIN_INTERVAL_SEC = 31.0  # у SPFA лимит ~2 преобразования в минуту

# Базовые паузы (сек) по типу блока; дальше экспонента 2^n и джиттер.
BLOCK_BASE_WAIT = {
    "rate_limit": 120.0,   # 403 {"too-many-requests": …} — лёгкий троттлинг
    "challenge": 300.0,    # 439 «проверка безопасности»
    "ip_block": 900.0,     # 429 / HTML «Доступ ограничен: проблема с IP»
}
BLOCK_MAX_WAIT = 6 * 3600.0

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
        base = self.retry_after if self.retry_after else BLOCK_BASE_WAIT.get(self.kind, 300.0)
        wait = min(base * (2 ** max(0, consecutive)), BLOCK_MAX_WAIT)
        return wait + random.uniform(15.0, 60.0)


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
                 timeout: float = 30.0):
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
        kwargs: Dict[str, Any] = {"impersonate": "chrome"}
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
        r = self._session.get(api_url, timeout=self.timeout, headers=headers, allow_redirects=False)
        self.last_status = r.status_code
        text = r.text or ""
        block = classify_block(r.status_code, r.headers, text)
        if block:
            self.total_blocked += 1
            raise block
        if r.status_code != 200:
            raise AvitoHttpError(r.status_code, text[:200])
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise RuntimeError(f"не JSON в ответе API: {text[:200]}") from exc
        self.total_ok += 1
        return payload

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass


# ===== конвертация публичной ссылки -> API URL =====

_spfa_last_call = 0.0
_spfa_lock = threading.Lock()


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
    if not any(k == "sort" for k, _ in query):
        query.append(("sort", "date"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def convert_url_to_api(url: str, cache_file: str, timeout: float = 25.0) -> Optional[str]:
    """
    Публичная ссылка Авито -> API URL (web/1/js/items).
    Ходим в бесплатный конвертер SPFA (лимит ~2/мин), результат кэшируем
    в cache_file навсегда. При неудаче возвращаем None (повторим позже).
    """
    global _spfa_last_call
    if is_valid_api_url(url):
        return _ensure_sort_date(url)

    from storage import (  # локальный импорт, чтобы не тянуть при тестах парсера
        load_state,
        save_state,
    )

    cache: Dict[str, str] = {}
    try:
        raw = load_state(cache_file, {}) or {}
        if isinstance(raw, dict):
            cache = {str(k): str(v) for k, v in raw.items()}
    except Exception as exc:
        logger.warning("Не удалось прочитать кэш API URL: %s", exc)
    if url in cache and is_valid_api_url(cache[url]):
        return _ensure_sort_date(cache[url])

    # Serialize SPFA calls so concurrent watcher startup cannot violate its rate limit.
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
                return None
            payload = r.json()
            api_url = payload.get("api_url") if payload.get("success") else None
            if not api_url:
                logger.warning("SPFA конвертер не вернул api_url для %s", url)
                return None
            api_url = _ensure_sort_date(str(api_url))
            if not is_valid_api_url(api_url):
                logger.warning("SPFA returned an invalid API URL for %s", url)
                return None
            cache[url] = api_url
            try:
                save_state(cache_file, cache)
            except Exception as exc:
                logger.warning("Не удалось сохранить кэш API URL: %s", exc)
            return api_url
        except Exception as exc:
            logger.warning("Ошибка конвертации URL %s: %s", url, exc)
            return None


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


def parse_api_items(payload: Dict[str, Any], limit: int = 20) -> List[Dict[str, Any]]:
    """
    JSON API Авито -> список словарей с ключами под dataclass Ad:
    ad_id, url, title, price, location, date_str, published_ts,
    description, image_url, seller_id, is_verified.
    """
    if not isinstance(payload, dict):
        return []
    catalog = payload.get("catalog")
    if not isinstance(catalog, dict):
        result = payload.get("result")
        catalog = result if isinstance(result, dict) else payload
    items = catalog.get("items")
    if not isinstance(items, list):
        return []

    ads: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type and str(item_type) in _NON_AD_TYPES:
            continue
        ad_id = item.get("id")
        url_path = item.get("urlPath")
        title = item.get("title")
        if not ad_id or not url_path or not title:
            continue  # сервисный блок, а не объявление

        price: Optional[int] = None
        pd = item.get("priceDetailed")
        if isinstance(pd, dict):
            value = pd.get("value")
            if isinstance(value, (int, float)) and value > 0:
                price = int(value)

        published_ts: Optional[float] = None
        sts = item.get("sortTimeStamp")
        if isinstance(sts, (int, float)) and sts > 0:
            published_ts = float(sts) / 1000.0

        location = ""
        geo = item.get("geo")
        if isinstance(geo, dict):
            location = _first_str(geo.get("formattedAddress"))
        if not location:
            addr = item.get("addressDetailed")
            if isinstance(addr, dict):
                location = _first_str(addr.get("locationName"))
        if not location:
            loc = item.get("location")
            if isinstance(loc, dict):
                location = _first_str(loc.get("name"))

        url = url_path if str(url_path).startswith("http") else f"https://www.avito.ru{url_path}"

        ads.append({
            "ad_id": str(ad_id),
            "url": url,
            "title": str(title),
            "price": price,
            "location": location,
            "date_str": "",
            "published_ts": published_ts,
            "description": str(item.get("description") or ""),
            "image_url": _item_image(item),
            "seller_id": str(item["sellerId"]) if item.get("sellerId") else None,
            "is_verified": bool(item.get("isVerifiedItem")),
        })
        if len(ads) >= limit:
            break
    return ads
