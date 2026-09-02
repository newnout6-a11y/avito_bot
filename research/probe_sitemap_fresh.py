# language: Python, file: probe_sitemap_fresh.py
# item_telefony_84_*.xml.gz: item-URL + lastmod. Свежесть — ключевой вопрос:
# если lastmod сегодня — sitemap можно использовать как discovery-канал.
import gzip
import re
import sys
import time
from datetime import datetime, timezone

from curl_cffi import requests as curl_requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

s = curl_requests.Session(impersonate="safari15_5")
s.headers.update({"accept": "*/*", "accept-language": "ru-RU,ru;q=0.9"})

now = datetime.now(timezone.utc)
print(f"сейчас UTC: {now.isoformat()}\n")

for idx in (0, 16):  # первый и последний блок
    url = f"https://www.avito.ru/sitemap/site/item_telefony_84_{idx}.xml.gz"
    r = s.get(url, timeout=90)
    if r.status_code != 200:
        print(f"[{idx}] {r.status_code}")
        continue
    raw = r.content or b""
    xml = gzip.decompress(raw).decode("utf-8", "replace")
    urls = re.findall(r"<loc>([^<]+)</loc>", xml)
    lastmods = re.findall(r"<lastmod>([^<]+)</lastmod>", xml)
    print(f"[item_{idx}] urls={len(urls)} lastmods={len(lastmods)} chars={len(xml)}")

    # Полные <url>-блоки для связи loc+lastmod
    blocks = re.findall(r"<url>(.*?)</url>", xml, re.S)
    parsed = []
    for b in blocks[:200000]:
        loc = re.search(r"<loc>([^<]+)</loc>", b)
        lm = re.search(r"<lastmod>([^<]+)</lastmod>", b)
        if loc:
            parsed.append((loc.group(1), lm.group(1) if lm else None))

    if parsed:
        ages = []
        for u, lm in parsed:
            if lm:
                try:
                    dt = datetime.fromisoformat(lm.replace("Z", "+00:00"))
                    ages.append((now - dt).total_seconds())
                except ValueError:
                    pass
        if ages:
            ages.sort()
            fresh_min = ages[0] / 60
            fresh_med = ages[len(ages) // 2] / 60
            fresh_max = ages[-1] / 3600
            print(f"  возраст lastmod: min={fresh_min:.1f} мин, медиана={fresh_med:.1f} мин, max={fresh_max:.1f} ч")
        print("  примеры (первые 5):")
        for u, lm in parsed[:5]:
            print(f"    {lm}  {u[:100]}")
    time.sleep(2)
