"""Domain models and pure helpers for Avito searches and notifications."""

import base64
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlparse, urlunparse

from bs4.element import Tag

from avito_settings import DISPLAY_TZ_NAME, DISPLAY_TZ_OFFSET_MIN
from avito_accounts import LicenseManager

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore


KEY_RE = re.compile(
    r"(?i)(?:^|\s)(?:ключ\s*:\s*)?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:\s|$)"
)


def _tz():
    if ZoneInfo:
        try:
            return ZoneInfo(DISPLAY_TZ_NAME)
        except Exception:
            pass
    return timezone(timedelta(minutes=DISPLAY_TZ_OFFSET_MIN))


def _fmt_dt(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(float(ts), _tz()).strftime("%d.%m.%Y %H:%M:%S")


def _parse_price_input(value: str) -> Optional[int]:
    normalized = (value or "").strip().replace("\u00a0", "").replace(" ", "").replace(".", "")
    return int(normalized) if normalized.isdigit() else None


def _br(text: str) -> str:
    value = text or ""
    value = value.replace("<br/>", "\n").replace("<br />", "\n").replace("<br>", "\n")
    return value.replace("\\n", "\n")


@dataclass
class SubscriberFilter:
    keywords_all: List[str] = field(default_factory=list)
    keywords_any: List[str] = field(default_factory=list)
    keywords_stop: List[str] = field(default_factory=list)
    price_min: Optional[int] = None
    price_max: Optional[int] = None


@dataclass
class Subscription:
    id: int
    user_id: int
    search_key: str
    url: str
    flt: SubscriberFilter = field(default_factory=SubscriberFilter)
    name: Optional[str] = None
    only_new: bool = True
    forward_chat_id: Optional[int] = None
    started_ts: float = field(default_factory=time.time)


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
    features: List[str] = field(default_factory=list)
    views: Optional[int] = None
    is_verified: bool = False


@dataclass
class FeedItem:
    ts: float
    title: str
    price: Optional[int]
    url: str
    date_str: str


def _normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if not parsed.scheme:
        parsed = parsed._replace(scheme="https")
    netloc = parsed.netloc or "www.avito.ru"
    query = urlencode(sorted(
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ))
    return urlunparse(parsed._replace(netloc=netloc, query=query))


def search_key_from_url(url: str) -> str:
    return _normalize_url(url)


def is_valid_avito_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    return parsed.scheme in {"http", "https"} and (host == "avito.ru" or host.endswith(".avito.ru"))


def avito_short_url(full_url: str) -> str:
    match = re.search(r"(?:/|_)(\d{7,})(?:[/?#]|$)", full_url)
    if match:
        return f"https://www.avito.ru/{match.group(1)}"
    parsed = urlparse(full_url)
    query = urlencode([
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() != "context" and not key.lower().startswith("utm_")
    ])
    return urlunparse((
        parsed.scheme or "https",
        parsed.netloc or "www.avito.ru",
        parsed.path,
        "",
        query,
        "",
    ))


def _extract_ad_id(url: str) -> str:
    match = re.search(r"(?:/|_)(\d{7,})(?:[/?#]|$)", url)
    return match.group(1) if match else url


def _attr_to_str(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], str):
        return value[0]
    return ""


def _get_text(tag: Optional[Tag]) -> str:
    if tag is None:
        return ""
    try:
        return tag.get_text(strip=True)
    except Exception:
        return str(tag) if tag else ""


def try_extract_filters_from_url(url: str) -> SubscriberFilter:
    result = SubscriberFilter()
    try:
        query = parse_qs(urlparse(url).query)
        query_lower = {key.lower(): values for key, values in query.items()}

        def query_price(keys: tuple[str, ...]) -> Optional[int]:
            for key in keys:
                values = query_lower.get(key.lower())
                if values:
                    parsed = _parse_price_input(unquote(values[0]))
                    if parsed is not None:
                        return parsed
            return None

        result.price_min = query_price(("pmin", "priceMin", "price_min", "priceFrom", "minPrice"))
        result.price_max = query_price(("pmax", "priceMax", "price_max", "priceTo", "maxPrice"))
        if result.price_min == 0:
            result.price_min = None

        if query.get("q"):
            result.keywords_all = [
                word for word in re.split(r"[,\s]+", unquote(query["q"][0]).strip()) if word
            ]

        encoded_filter = query.get("f", [None])[0]
        if encoded_filter:
            padding = "=" * ((4 - len(encoded_filter.strip()) % 4) % 4)
            decoded = base64.urlsafe_b64decode(encoded_filter.strip() + padding).decode(
                "utf-8", errors="ignore"
            )
            json_start = decoded.find("{")
            payload = json.loads(decoded[json_start:]) if json_start >= 0 else None
            text_parts: List[str] = []
            if isinstance(payload, dict):
                encoded_min = _parse_price_input(str(payload.get("from", "")))
                encoded_max = _parse_price_input(str(payload.get("to", "")))
                if result.price_min is None and encoded_min not in (None, 0):
                    result.price_min = encoded_min
                if result.price_max is None and encoded_max is not None:
                    result.price_max = encoded_max

                def pick(obj, keys):
                    for key in keys:
                        value = obj.get(key)
                        if isinstance(value, str) and value:
                            text_parts.append(value)

                pick(payload, ["brand", "model", "storage", "keyword", "q"])
                if isinstance(payload.get("params"), list):
                    for parameter in payload["params"]:
                        if isinstance(parameter, dict):
                            pick(parameter, ["brand", "model", "storage", "value", "name"])

            known_words = {word.lower() for word in result.keywords_all}
            for text_part in text_parts:
                for word in re.split(r"[,\s/]+", str(text_part)):
                    word = word.strip()
                    if word and word.lower() not in known_words:
                        result.keywords_all.append(word)
                        known_words.add(word.lower())
    except Exception:
        pass
    return result
