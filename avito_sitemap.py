# -*- coding: utf-8 -*-
"""
Sitemap-канал Авито: бесплатный discovery item-URL вне Qrator.

Живые пробы (08.2026): при активном IP-бане (429 на страницах и API)
robots.txt и sitemap/*.xml.gz продолжают отдавать 200 — это CDN-статика
для поисковиков, фаервол её не трогает.

Структура: https://www.avito.ru/sitemap/index.xml перечисляет ~4.3k карт,
в т.ч. item_<category>_<catId>_<N>.xml.gz — item-URL с <lastmod>.
Последний блок N каждой категории — инкрементальный (свежие поступления,
в пробах ~13–14 ч; блоки 0..N-1 — базовый дамп со старьём).

Зачем боту: когдаWatcher упирается в ip_block, свежие item-URL из sitemap
помечаются в seen — после разбана бот не засыпает пользователя накопившимся
старьём, окно пропущенных объявлений закрывается без потерь.
"""

import gzip
import logging
import re
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)

SITEMAP_INDEX = "https://www.avito.ru/sitemap/index.xml"
_ITEM_MAP_RE = re.compile(r"/sitemap/site/item_([a-z0-9_]+?)_(\d+)_(\d+)\.xml\.gz$")
_LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.S)
_LOC_IN_BLOCK_RE = re.compile(r"<loc>([^<]+)</loc>")
_LASTMOD_RE = re.compile(r"<lastmod>([^<]+)</lastmod>")

_index_cache: Dict[str, List[str]] = {}
_index_cache_lock = threading.Lock()


@dataclass(frozen=True)
class ItemSitemap:
    category_slug: str
    category_id: int
    block: int
    url: str


def _new_session() -> "curl_requests.Session":
    s = curl_requests.Session(impersonate="safari15_5")
    s.headers.update({"accept": "*/*", "accept-language": "ru-RU,ru;q=0.9"})
    return s


def fetch_index(timeout: float = 30.0, use_cache: bool = True) -> List[ItemSitemap]:
    """Все item-саймапы из index.xml (с кэшем процесса)."""
    with _index_cache_lock:
        if use_cache and _index_cache:
            return _decode_index(_index_cache["locs"])
    s = _new_session()
    r = s.get(SITEMAP_INDEX, timeout=timeout)
    r.raise_for_status()
    locs = _LOC_RE.findall(r.text or "")
    with _index_cache_lock:
        _index_cache["locs"] = locs
    return _decode_index(locs)


def _decode_index(locs: List[str]) -> List[ItemSitemap]:
    out = []
    for loc in locs:
        m = _ITEM_MAP_RE.search(loc)
        if m:
            out.append(ItemSitemap(m.group(1), int(m.group(2)), int(m.group(3)), loc))
    return out


def latest_item_sitemaps(category_slug: Optional[str] = None,
                          category_id: Optional[int] = None,
                          timeout: float = 30.0) -> List[ItemSitemap]:
    """Последний (самый свежий) блок для каждой категории; опционально — одной."""
    maps = fetch_index(timeout=timeout)
    best: Dict[Tuple[str, int], ItemSitemap] = {}
    for sm in maps:
        if category_slug and sm.category_slug != category_slug:
            continue
        if category_id and sm.category_id != category_id:
            continue
        key = (sm.category_slug, sm.category_id)
        if key not in best or sm.block > best[key].block:
            best[key] = sm
    return sorted(best.values(), key=lambda m: (m.category_slug, m.category_id))


def fetch_fresh_items(category_slug: Optional[str] = None,
                      category_id: Optional[int] = None,
                      timeout: float = 90.0,
                      max_blocks: int = 1) -> List[Tuple[str, str]]:
    """[(item_url, lastmod_iso)] из свежих блоков sitemap категории."""
    s = _new_session()
    # Свежие блоки: берём max_blocks последних по номеру.
    maps_by_key: Dict[Tuple[str, int], List[ItemSitemap]] = {}
    all_maps = [m for m in fetch_index(timeout=timeout)
                if (not category_slug or m.category_slug == category_slug)
                and (not category_id or m.category_id == category_id)]
    for m in all_maps:
        maps_by_key.setdefault((m.category_slug, m.category_id), []).append(m)
    out: List[Tuple[str, str]] = []
    for key, blocks in maps_by_key.items():
        blocks.sort(key=lambda m: m.block)
        for sm in blocks[-max_blocks:]:
            try:
                r = s.get(sm.url, timeout=timeout)
                r.raise_for_status()
                xml = gzip.decompress(r.content or b"").decode("utf-8", "replace")
            except Exception as exc:
                logger.warning("Sitemap %s: %s", sm.url, exc)
                continue
            for block_xml in _BLOCK_RE.findall(xml):
                loc = _LOC_IN_BLOCK_RE.search(block_xml)
                lm = _LASTMOD_RE.search(block_xml)
                if loc:
                    out.append((loc.group(1), lm.group(1) if lm else ""))
    return out


def mark_seen_from_sitemap(category_slug: Optional[str] = None,
                           category_id: Optional[int] = None,
                           seen: Optional[Dict[str, float]] = None,
                           max_blocks: int = 1) -> int:
    """Помечает item-ID из sitemap в seen (по времени сейчас).

    Возвращает число помеченных. Ошибки сети молча проглатываются:
    это best-effort оптимизация, а не критичный путь.
    """
    import time as _time
    try:
        items = fetch_fresh_items(category_slug, category_id, max_blocks=max_blocks)
    except Exception as exc:
        logger.warning("Sitemap fetch не удался: %s", exc)
        return 0
    if seen is None:
        return 0
    now = _time.time()
    marked = 0
    for url, _lastmod in items:
        ad_id = url.rstrip("/").rsplit("_", 1)[-1]
        if ad_id.isdigit() and ad_id not in seen:
            seen[ad_id] = now
            marked += 1
    if marked:
        logger.info("Sitemap: помечено %d свежих item-ID в seen (%s/%s)",
                    marked, category_slug, category_id)
    return marked
