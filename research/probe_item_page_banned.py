# language: Python, file: probe_item_page_banned.py
# IP в бане, но sitemap открыт. Отдаёт ли Avito саму item-страницу (HTML)
# при бане? Если да — у нас полный обходной канал.
import sys

from curl_cffi import requests as curl_requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

s = curl_requests.Session(impersonate="safari15_5")
s.headers.update({
    "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "accept-language": "ru-RU,ru;q=0.9",
    "sec-fetch-dest": "document", "sec-fetch-mode": "navigate", "sec-fetch-site": "none",
    "upgrade-insecure-requests": "1",
})

# Свежие URL из sitemap (lastmod 2026-08-27T03:24-25Z)
targets = [
    "https://www.avito.ru/irkutsk/telefony/chasy_apple_watch_8_45_mm_8330204224",
    "https://www.avito.ru/chelyabinsk/telefony/iphone_16_128_gb_8323342236",
]

for url in targets:
    try:
        r = s.get(url, timeout=30, allow_redirects=False)
        body = (r.content or b"")[:150].decode("utf-8", "replace").replace("\n", " ")
        print(f"{r.status_code} bytes={len(r.content or b'')} loc={r.headers.get('location', '-')[:60]}")
        print(f"  {url.rsplit('/', 1)[-1]}")
        print(f"  head: {body[:130]}")
    except Exception as e:
        print(f"ERR {url[-20:]}: {type(e).__name__}: {str(e)[:80]}")
