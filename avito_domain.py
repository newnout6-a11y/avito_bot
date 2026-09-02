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


AVITO_LOCATIONS = {
    "all": "Вся Россия",
    # Города федерального значения
    "moskva": "Москва",
    "sankt-peterburg": "Санкт-Петербург",
    "sevastopol": "Севастополь",
    # Столицы регионов и крупные города
    "novosibirsk": "Новосибирск",
    "ekaterinburg": "Екатеринбург",
    "kazan": "Казань",
    "nizhniy_novgorod": "Нижний Новгород",
    "chelyabinsk": "Челябинск",
    "samara": "Самара",
    "ufa": "Уфа",
    "rostov-na-donu": "Ростов-на-Дону",
    "krasnodar": "Краснодар",
    "omsk": "Омск",
    "voronezh": "Воронеж",
    "perm": "Пермь",
    "volgograd": "Волгоград",
    "krasnoyarsk": "Красноярск",
    "saratov": "Саратов",
    "tyumen": "Тюмень",
    "tolyatti": "Тольятти",
    "barnaul": "Барнаул",
    "izhevsk": "Ижевск",
    "mahachkala": "Махачкала",
    "habarovsk": "Хабаровск",
    "ulyanovsk": "Ульяновск",
    "irkutsk": "Иркутск",
    "vladivostok": "Владивосток",
    "yaroslavl": "Ярославль",
    "tomsk": "Томск",
    "stavropol": "Ставрополь",
    "kemerovo": "Кемерово",
    "naberezhnye_chelny": "Набережные Челны",
    "orenburg": "Оренбург",
    "novokuznetsk": "Новокузнецк",
    "balashiha": "Балашиха",
    "ryazan": "Рязань",
    "cheboksary": "Чебоксары",
    "penza": "Пенза",
    "lipetsk": "Липецк",
    "kaliningrad": "Калининград",
    "astrahan": "Астрахань",
    "tula": "Тула",
    "kirov": "Киров",
    "sochi": "Сочи",
    "kursk": "Курск",
    "ulan-ude": "Улан-Удэ",
    "tver": "Тверь",
    "magnitogorsk": "Магнитогорск",
    "surgut": "Сургут",
    "bryansk": "Брянск",
    "ivanovo": "Иваново",
    "yakutsk": "Якутск",
    "vladimir": "Владимир",
    "belgorod": "Белгород",
    "nizhniy_tagil": "Нижний Тагил",
    "kaluga": "Калуга",
    "chita": "Чита",
    "smolensk": "Смоленск",
    "volzhskiy": "Волжский",
    "kurgan": "Курган",
    "cherepovets": "Череповец",
    "orel": "Орёл",
    "saransk": "Саранск",
    "vologda": "Вологда",
    "podolsk": "Подольск",
    "vladikavkaz": "Владикавказ",
    "tambov": "Тамбов",
    "murmansk": "Мурманск",
    "petrozavodsk": "Петрозаводск",
    "nizhnevartovsk": "Нижневартовск",
    "kostroma": "Кострома",
    "yoshkar-ola": "Йошкар-Ола",
    "novorossiysk": "Новороссийск",
    "sterlitamak": "Стерлитамак",
    "himki": "Химки",
    "taganrog": "Таганрог",
    "mytischi": "Мытищи",
    "syktyvkar": "Сыктывкар",
    "komsomolsk-na-amure": "Комсомольск-на-Амуре",
    "nizhnekamsk": "Нижнекамск",
    "nalchik": "Нальчик",
    "shahty": "Шахты",
    "dzerzhinsk": "Дзержинск",
    "engels": "Энгельс",
    "orsk": "Орск",
    "bratsk": "Братск",
    "velikiy_novgorod": "Великий Новгород",
    "korolev": "Королёв",
    "staryy_oskol": "Старый Оскол",
    "angarsk": "Ангарск",
    "pskov": "Псков",
    "lyubertsy": "Люберцы",
    "yuzhno-sahalinsk": "Южно-Сахалинск",
    "biysk": "Бийск",
    "prokopevsk": "Прокопьевск",
    "abakan": "Абакан",
    "armavir": "Армавир",
    "balakovo": "Балаково",
    "norilsk": "Норильск",
    "rybinsk": "Рыбинск",
    "severodvinsk": "Северодвинск",
    "petropavlovsk-kamchatskiy": "Петропавловск-Камчатский",
    "krasnogorsk": "Красногорск",
    "ussuriysk": "Уссурийск",
    "volgodonsk": "Волгодонск",
    "novocherkassk": "Новочеркасск",
    "syzran": "Сызрань",
    "zlatoust": "Златоуст",
    "kamensk-uralskiy": "Каменск-Уральский",
    "elektrostal": "Электросталь",
    "almetevsk": "Альметьевск",
    "salavat": "Салават",
    "miass": "Миасс",
    "nahodka": "Находка",
    "kopeysk": "Копейск",
    "pyatigorsk": "Пятигорск",
    "rubtsovsk": "Рубцовск",
    "berezniki": "Березники",
    "kolomna": "Коломна",
    "maykop": "Майкоп",
    "odintsovo": "Одинцово",
    "kovrov": "Ковров",
    "hasavyurt": "Хасавюрт",
    "kislovodsk": "Кисловодск",
    "nefteyugansk": "Нефтеюганск",
    "bataysk": "Батайск",
    "novomoskovsk": "Новомосковск",
    "serpuhov": "Серпухов",
    "cherkessk": "Черкесск",
    "pervouralsk": "Первоуральск",
    "neftekamsk": "Нефтекамск",
    "novocheboksarsk": "Новочебоксарск",
    "orehovo-zuevo": "Орехово-Зуево",
    "derbent": "Дербент",
    "dimitrovgrad": "Димитровград",
    "nevinnomyssk": "Невинномысск",
    "kamyshin": "Камышин",
    "kyzyl": "Кызыл",
    "novyy_urengoy": "Новый Уренгой",
    "murom": "Муром",
    "obninsk": "Обнинск",
    "nazran": "Назрань",
    "kaspiysk": "Каспийск",
    "essentuki": "Ессентуки",
    "ramenskoe": "Раменское",
    "berdsk": "Бердск",
    "serov": "Серов",
    "votkinsk": "Воткинск",
    "seversk": "Северск",
    "zhukovskiy": "Жуковский",
    "noyabrsk": "Ноябрьск",
    "hanty-mansiysk": "Ханты-Мансийск",
    "achinsk": "Ачинск",
    "elets": "Елец",
    "zheleznogorsk": "Железногорск",
    "anapa": "Анапа",
    "gelendzhik": "Геленджик",
    "domodedovo": "Домодедово",
    "schelkovo": "Щёлково",
    "dolgoprudnyy": "Долгопрудный",
    "reutov": "Реутов",
    "pushkino": "Пушкино",
    "lobnya": "Лобня",
    "vidnoe": "Видное",
    "murino": "Мурино",
    "kudrovo": "Кудрово",
    "gatchina": "Гатчина",
    "vyborg": "Выборг",
    "vsevolozhsk": "Всеволожск",
    "kerch": "Керчь",
    "simferopol": "Симферополь",
    "evpatoriya": "Евпатория",
    "yalta": "Ялта",
    "feodosiya": "Феодосия",
    "gorno-altaysk": "Горно-Алтайск",
    "birobidzhan": "Биробиджан",
    "magadan": "Магадан",
    "naryan-mar": "Нарьян-Мар",
    "anadyr": "Анадырь",
    "salehard": "Салехард",
    "elista": "Элиста",
}

AVITO_CATEGORIES = {
    "telefony": "Телефоны",
    "mobilnye_telefony": "Мобильные телефоны",
    "avtomobili": "Автомобили",
    "kvartiry": "Квартиры",
    "doma_dachi_kottedzhi": "Дома, дачи, коттеджи",
    "noutbuki": "Ноутбуки",
    "audio_i_video": "Аудио и видео",
    "bytovaya_tehnika": "Бытовая техника",
    "tovary_dlya_kompyutera": "Компьютерная техника",
    "planshety_i_elektronnye_knigi": "Планшеты",
    "odezhda_obuv_aksessuary": "Одежда и обувь",
    "chasy_i_ukrasheniya": "Часы и украшения",
    "remont_i_stroitelstvo": "Ремонт и строительство",
    "mebel_i_interer": "Мебель и интерьер",
    "posuda_i_tovary_dlya_kuhni": "Посуда и товары для кухни",
    "rasteniya": "Растения",
    "sobaki": "Собаки",
    "koshki": "Кошки",
    "velosipedy": "Велосипеды",
    "knigi_i_zhurnaly": "Книги и журналы",
    "muzykalnye_instrumenty": "Музыкальные инструменты",
    "sport_i_otdyh": "Спорт и отдых",
    "igry_pristavki_i_programmy": "Игры и приставки",
    "nastolnye_kompyutery": "Компьютеры",
    "fototehnika": "Фототехника",
}

AVITO_COLORS = {
    "belyy": "Белый",
    "chernyy": "Чёрный",
    "siniy": "Синий",
    "krasnyy": "Красный",
    "seryy": "Серый",
    "zelenyy": "Зелёный",
    "zolotoy": "Золотой",
    "serebristyy": "Серебристый",
    "fioletovyy": "Фиолетовый",
    "rozovyy": "Розовый",
    "goluboy": "Голубой",
    "zheltyy": "Жёлтый",
    "bezhevyy": "Бежевый",
    "oranzhevyy": "Оранжевый",
    "korichnevyy": "Коричневый",
    "biryuzovyy": "Бирюзовый",
}

AVITO_SORTS = {
    "104": "По дате",
    "101": "По умолчанию",
    "1": "Дешевле",
    "2": "Дороже",
    "date": "По дате",
    "default": "По умолчанию",
    "price_asc": "Дешевле",
    "price_desc": "Дороже",
}

_TRANSLIT_RULES = (
    ("shch", "щ"), ("sch", "щ"), ("yo", "ё"), ("zh", "ж"),
    ("ch", "ч"), ("sh", "ш"), ("kh", "х"), ("ts", "ц"),
    ("yu", "ю"), ("ya", "я"), ("iy", "ий"), ("yy", "ый"),
)

_SINGLE_TRANSLIT = {
    "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "е",
    "z": "з", "i": "и", "j": "й", "k": "к", "l": "л", "m": "м",
    "n": "н", "o": "о", "p": "п", "r": "р", "s": "с", "t": "т",
    "u": "у", "f": "ф", "h": "х", "c": "ц", "y": "ы", "w": "в",
    "x": "кс", "q": "к",
}


def _translit_location(slug: str) -> str:
    res = slug.lower().replace("_", " ").replace("-", " - ")
    for lat, cyr in _TRANSLIT_RULES:
        res = res.replace(lat, cyr)
    chars = [_SINGLE_TRANSLIT.get(c, c) for c in res]
    res = "".join(chars).replace(" - ", "-")
    return " ".join(w.capitalize() for w in res.split())


def translit_to_cyrillic(text: str) -> str:
    """Best-effort latin->cyrillic transliteration for fuzzy keyword matching.

    'samsung' -> 'самсунг'. Both the keyword and the ad text are passed through
    this, so a latin query matches a cyrillic listing and vice versa.
    """
    res = (text or "").casefold()
    for lat, cyr in _TRANSLIT_RULES:
        res = res.replace(lat, cyr)
    return "".join(_SINGLE_TRANSLIT.get(ch, ch) for ch in res)


def keyword_in_text(keyword: str, haystack_casefold: str, haystack_translit: str) -> bool:
    """True if keyword occurs in the text, ignoring case and latin/cyrillic script.

    ``haystack_casefold`` and ``haystack_translit`` are the text pre-normalised
    once by the caller (``text.casefold()`` and ``translit_to_cyrillic(text)``).
    """
    word = (keyword or "").strip().casefold()
    if not word:
        return False
    if word in haystack_casefold:
        return True
    return translit_to_cyrillic(word) in haystack_translit


def extract_url_metadata(url_str: str) -> dict[str, object]:
    parsed = urlparse(url_str)
    path_parts = [p for p in (parsed.path or "").strip("/").split("/") if p]

    loc: Optional[str] = None
    cats: list[str] = []
    color: Optional[str] = None

    if path_parts:
        first = path_parts[0].lower()
        if first in AVITO_LOCATIONS:
            loc = AVITO_LOCATIONS[first]
            path_parts = path_parts[1:]
        elif not any(first.startswith(c) for c in AVITO_CATEGORIES):
            loc = _translit_location(first)
            path_parts = path_parts[1:]

    for part in path_parts:
        clean_part = re.sub(r"-ASgBA[A-Za-z0-9_-]+$", "", part)
        color_match = re.match(r"^([a-z]+)-(.*)$", clean_part)
        if color_match and color_match.group(1) in AVITO_COLORS:
            color = AVITO_COLORS[color_match.group(1)]
            clean_part = color_match.group(2)
        elif clean_part in AVITO_COLORS:
            color = AVITO_COLORS[clean_part]
            continue

        if clean_part in AVITO_CATEGORIES:
            cats.append(AVITO_CATEGORIES[clean_part])
        elif clean_part:
            cats.append(clean_part.replace("_", " ").title())

    qs = parse_qs(parsed.query)
    delivery = qs.get("cd", ["0"])[0] == "1"
    sort_value = str((qs.get("s") or qs.get("sort") or [""])[0]).casefold()
    sort_title = AVITO_SORTS.get(sort_value)
    if sort_title is None:
        sort_title = f"Неизвестная ({sort_value})" if sort_value else "По умолчанию"

    return {
        "location": loc,
        "category": " / ".join(cats) if cats else None,
        "color": color,
        "delivery": delivery,
        "sort_title": sort_title,
    }


@dataclass(frozen=True)
class SearchSpec:
    canonical_url: str
    display_url: str
    search_key: str
    kind: Literal["search", "item"]
    filters: SubscriberFilter
    warnings: tuple[str, ...] = ()
    location: Optional[str] = None
    category: Optional[str] = None
    color: Optional[str] = None
    delivery: bool = False
    sort_title: Optional[str] = None


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
    published_exact: bool = False
    is_promoted: bool = False
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
            "locationid", "radius", "searchradius", "geocoords", "cd",
        } and not key.casefold().startswith("params["):
            warnings.append(f"Неподдерживаемый параметр: {key}")
    pairs.sort(key=lambda item: (item[0], item[1]))
    netloc = host
    canonical = urlunparse(("https", netloc, parsed.path or "/", "", urlencode(pairs, doseq=True), ""))
    # A numeric id in the path denotes an item card, not a search.
    kind: Literal["search", "item"] = "item" if re.search(r"(?:^|[_/-])\d{7,}(?:$|[/?_-])", parsed.path) else "search"
    filters, filter_warnings = parse_filters(canonical)
    warnings.extend(filter_warnings)
    meta = extract_url_metadata(canonical)
    return SearchSpec(
        canonical,
        original or canonical,
        canonical,
        kind,
        filters,
        tuple(dict.fromkeys(warnings)),
        location=meta.get("location"),  # type: ignore
        category=meta.get("category"),  # type: ignore
        color=meta.get("color"),  # type: ignore
        delivery=bool(meta.get("delivery")),
        sort_title=meta.get("sort_title"),  # type: ignore
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
    # Avito uses `~` as a separator between independent filter segments.
    # Each segment is a separate base64-encoded filter. The segment containing
    # JSON price data may have a variable-length binary prefix before the JSON.
    # Try to decode each segment and search for JSON within it.
    segments = [s for s in token.split("~") if s]

    for segment in segments:
        # Try the full segment first
        for start_offset in range(0, min(len(segment), 10)):
            test_segment = segment[start_offset:]

            if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", test_segment):
                continue

            test_segment = test_segment.rstrip("=")
            padding = "=" * ((4 - len(test_segment) % 4) % 4)

            try:
                raw_decoded = base64.urlsafe_b64decode(test_segment + padding)
            except (binascii.Error, ValueError):
                continue

            json_start = raw_decoded.find(b"{")
            if json_start < 0:
                continue

            try:
                payload = json.loads(raw_decoded[json_start:].decode("utf-8"))
                if isinstance(payload, dict):
                    return payload
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue

    raise ValueError("JSON object not found")


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
