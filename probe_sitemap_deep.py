# language: Python, file:probe_sitemap_deep.py
# Глубина sitemap: index -> category maps -> item URLs. Насколько свежие?
import re
import sys
import time

from curl_cffi import requests as curl_requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

s = curl_requests.Session(impersonate="safari15_5")
s.headers.update({"accept": "application/xml,text/xml,*/*", "accept-language": "ru-RU,ru;q=0.9"})

r = s.get("https://www.avito.ru/sitemap/index.xml", timeout=30, allow_redirects=False)
print(f"sitemap index: {r.status_code} bytes={len(r.content or b'')}")
if r.status_code != 200:
    head = (r.content or b"")[:300].decode("utf-8", "replace")
    print(f"head: {head}")
    sys.exit(0)

text = r.text or ""
locs = re.findall(r"<loc>([^<]+)</loc>", text)
print(f"sub-sitemaps: {len(locs)}")
for loc in locs[:25]:
    print(f"  {loc}")
if len(locs) > 25:
    print(f"  ... и ещё {len(locs) - 25}")

# Ищем sitemap с item-URL (не категории): обычно items*.xml или похожее
item_maps = [u for u in locs if re.search(r"item|listing|ad|offer|page", u, re.I)]
print(f"\nкандидаты с item-URL: {len(item_maps)}")
for m in item_maps[:5]:
    print(f"  {m}")

# Пробуем первый кандидат: что внутри, есть ли lastmod
if item_maps:
    time.sleep(2)
    r2 = s.get(item_maps[0], timeout=60, allow_redirects=False)
    print(f"\n{item_maps[0]}: {r2.status_code} bytes={len(r2.content or b'')}")
    if r2.status_code == 200:
        t2 = r2.text or ""
        urls = re.findall(r"<loc>([^<]+)</loc>", t2)
        lastmods = re.findall(r"<lastmod>([^<]+)</lastmod>", t2)
        print(f"urls: {len(urls)}, lastmods: {len(lastmods)}")
        for u, lm in list(zip(urls, lastmods))[:10]:
            print(f"  {lm}  {u[:100]}")
        if urls and not lastmods:
            print("примеры URL без lastmod:")
            for u in urls[:10]:
                print(f"  {u[:110]}")
