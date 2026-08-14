"""Domain models and pure helpers for Avito searches and notifications."""

import base64
import binascii
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Literal, Optional
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from bs4.element import Tag

from avito_accounts import LicenseManager  # noqa: F401
from avito_settings import DISPLAY_TZ_NAME, DISPLAY_TZ_OFFSET_MIN

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


@dataclass(frozen=True)
class SearchSpec:
    canonical_url: str
    display_url: str
    search_key: str
    kind: Literal["search", "item"]
    filters: SubscriberFilter
    warnings: tuple[str, ...] = ()


# Backwards-compatible public name used by existing integrations.
ParsedAvitoUrl = SearchSpec


@dataclass
class Subscription:
    id: int
    user_id: int
    search_key: str
    url: str
    flt: SubscriberFilter = field(default_factory=SubscriberFilter)
    original_url: Optional[str] = None
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


_TRAILING_URL_PUNCT = ".,;:!?)]}>'\"»"


def _clean_url_text(url: str) -> str:
    value = (url or "").strip().strip(_TRAILING_URL_PUNCT)
    if not re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I):
        value = "https://" + value
    return value


def parse_avito_url(url: str) -> SearchSpec:
    """Validate and canonically normalize a public Avito URL."""
    original = (url or "").strip()
    cleaned = _clean_url_text(original)
    parsed = urlparse(cleaned)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("Поддерживаются только HTTP и HTTPS ссылки")
    host = (parsed.hostname or "").rstrip(".").casefold()
    if not host or not (host == "avito.ru" or host.endswith(".avito.ru")):
        raise ValueError("Разрешены только avito.ru и его поддомены")
    if parsed.username or parsed.password:
        raise ValueError("URL не должен содержать credentials")
    if parsed.port is not None and parsed.port not in (80, 443):
        raise ValueError("Нестандартный порт в URL не поддерживается")
    if parsed.fragment:
        raise ValueError("Фрагмент URL не поддерживается")
    if host == "www.avito.ru":
        host = "avito.ru"
    pairs = []
    warnings: list[str] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.casefold().startswith("utm_"):
            continue
        pairs.append((key, value))
        if key.casefold() not in {
            "q", "f", "pmin", "pmax", "pricemin", "pricemax",
            "price_min", "price_max", "pricefrom", "priceto",
            "minprice", "maxprice", "context", "s", "sort", "categoryid",
            "locationid", "radius", "searchradius", "geocoords",
        } and not key.casefold().startswith("params["):
            warnings.append(f"Неподдерживаемый параметр: {key}")
    pairs.sort(key=lambda item: (item[0], item[1]))
    netloc = host
    canonical = urlunparse(("https", netloc, parsed.path or "/", "", urlencode(pairs, doseq=True), ""))
    # A numeric id in the path denotes an item card, not a search.
    kind: Literal["search", "item"] = "item" if re.search(r"(?:^|[_/-])\d{7,}(?:$|[/?_-])", parsed.path) else "search"
    filters, filter_warnings = parse_filters(canonical)
    warnings.extend(filter_warnings)
    return SearchSpec(
        canonical,
        original or canonical,
        canonical,
        kind,
        filters,
        tuple(dict.fromkeys(warnings)),
    )


def _normalize_url(url: str) -> str:
    return parse_avito_url(url).canonical_url


def search_key_from_url(url: str) -> str:
    return _normalize_url(url)


def is_valid_avito_url(url: str) -> bool:
    try:
        parse_avito_url(url)
        return True
    except (TypeError, ValueError):
        return False


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


_PRICE_MIN_KEYS = ("pmin", "pricemin", "price_min", "pricefrom", "minprice")
_PRICE_MAX_KEYS = ("pmax", "pricemax", "price_max", "priceto", "maxprice")
_FILTER_TEXT_KEYS = ("brand", "model", "storage", "keyword", "q")
_PARAM_TEXT_KEYS = ("brand", "model", "storage", "value", "name")


def normalize_filter(filters: SubscriberFilter) -> SubscriberFilter:
    """Return a detached filter with stable case-insensitive word lists."""
    normalized = SubscriberFilter(price_min=filters.price_min, price_max=filters.price_max)
    for attr in ("keywords_all", "keywords_any", "keywords_stop"):
        seen: set[str] = set()
        words: list[str] = []
        for value in getattr(filters, attr, ()):
            token = str(value).strip()
            folded = token.casefold()
            if token and folded not in seen:
                seen.add(folded)
                words.append(token)
        setattr(normalized, attr, words)
    if normalized.price_min == 0:
        normalized.price_min = None
    return normalized


def _first_query_price(query: dict[str, list[str]], keys: tuple[str, ...]) -> Optional[int]:
    for key in keys:
        values = query.get(key)
        if values:
            parsed = _parse_price_input(values[0])
            if parsed is not None:
                return parsed
    return None


def _parse_query_filters(query: dict[str, list[str]]) -> SubscriberFilter:
    result = SubscriberFilter(
        price_min=_first_query_price(query, _PRICE_MIN_KEYS),
        price_max=_first_query_price(query, _PRICE_MAX_KEYS),
    )
    for value in query.get("q", []):
        result.keywords_all.extend(
            word for word in re.split(r"[,\s]+", value.strip()) if word
        )
    return result


def _decode_filter_payload(encoded_filter: str) -> dict[str, object]:
    token = encoded_filter.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", token):
        raise ValueError("malformed base64")
    token = token.rstrip("=")
    padding = "=" * ((4 - len(token) % 4) % 4)
    try:
        raw_decoded = base64.urlsafe_b64decode(token + padding)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("malformed base64") from exc
    json_start = raw_decoded.find(b"{")
    if json_start < 0:
        raise ValueError("JSON object not found")
    try:
        payload = json.loads(raw_decoded[json_start:].decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError("invalid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("malformed JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("decoded filter is not an object")
    return payload


def _parse_encoded_filter(encoded_filter: str) -> SubscriberFilter:
    payload = _decode_filter_payload(encoded_filter)
    result = SubscriberFilter(
        price_min=_parse_price_input(str(payload.get("from", ""))),
        price_max=_parse_price_input(str(payload.get("to", ""))),
    )
    text_parts: list[str] = []

    def pick(obj: dict[str, object], keys: tuple[str, ...]) -> None:
        for key in keys:
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                text_parts.append(value)

    pick(payload, _FILTER_TEXT_KEYS)
    parameters = payload.get("params")
    if parameters is not None and not isinstance(parameters, list):
        raise ValueError("params must be a list")
    for parameter in parameters or []:
        if not isinstance(parameter, dict):
            raise ValueError("params entries must be objects")
        pick(parameter, _PARAM_TEXT_KEYS)
    for text_part in text_parts:
        result.keywords_all.extend(
            word for word in re.split(r"[,\s/]+", text_part) if word.strip()
        )
    return result


def _merge_filters(primary: SubscriberFilter, fallback: SubscriberFilter) -> SubscriberFilter:
    merged = SubscriberFilter(
        keywords_all=[*primary.keywords_all, *fallback.keywords_all],
        keywords_any=[*primary.keywords_any, *fallback.keywords_any],
        keywords_stop=[*primary.keywords_stop, *fallback.keywords_stop],
        price_min=primary.price_min if primary.price_min is not None else fallback.price_min,
        price_max=primary.price_max if primary.price_max is not None else fallback.price_max,
    )
    return normalize_filter(merged)


def parse_filters(url: str) -> tuple[SubscriberFilter, list[str]]:
    warnings: list[str] = []
    try:
        raw_query = parse_qs(urlparse(url).query, keep_blank_values=True)
        query = {key.casefold(): values for key, values in raw_query.items()}
    except (TypeError, ValueError, AttributeError) as exc:
        return SubscriberFilter(), [f"Некорректные параметры фильтра: {exc}"]

    result = _parse_query_filters(query)
    encoded_values = query.get("f", [])
    for encoded_filter in encoded_values:
        if not encoded_filter:
            continue
        try:
            result = _merge_filters(result, _parse_encoded_filter(encoded_filter))
        except ValueError as exc:
            warnings.append(f"Не удалось разобрать фильтр f: {exc}")
    result = normalize_filter(result)
    if (
        result.price_min is not None
        and result.price_max is not None
        and result.price_min > result.price_max
    ):
        warnings.append("Минимальная цена больше максимальной")
    return result, warnings


def try_extract_filters_from_url(url: str) -> SubscriberFilter:
    try:
        return parse_filters(url)[0]
    except (ValueError, TypeError):
        return SubscriberFilter()
