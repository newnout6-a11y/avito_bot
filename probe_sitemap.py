# language: Python, file: probe_sitemap.py
# Sitemap-вектор: Avito сам отдаёт поисковикам sitemap с item-URL.
# Если канал открыт — бесплатный discovery свежих объявлений без поиска.
import re
import sys

from curl_cffi import requests as curl_requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

s = curl_requests.Session(impersonate="safari15_5")
s.headers.update({"accept": "*/*", "accept-language": "ru-RU,ru;q=0.9"})

# Шаг 1: robots.txt
r = s.get("https://www.avito.ru/robots.txt", timeout=25, allow_redirects=False)
print(f"robots.txt: {r.status_code} bytes={len(r.content or b'')}")
if r.status_code == 200:
    text = r.text or ""
    print("--- robots.txt (первые 2000) ---")
    print(text[:2000])
    sitemaps = re.findall(r"^Sitemap:\s*(\S+)", text, re.M)
    print(f"\nsitemaps declared: {sitemaps}")
