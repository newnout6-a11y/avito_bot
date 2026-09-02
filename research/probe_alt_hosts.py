# language: Python, file: probe_alt_hosts.py
# Альтернативные хосты и мобильный API: живы ли они без Qrator-challenge?
import time

from curl_cffi import requests as curl_requests

MOBILE_KEY = "af0deccbgcgidddjgnvljitntccdduijhdinfgjgfjir"

targets = [
    ("https://m.avito.ru/", "m.avito home", {}),
    ("https://www.avito.ru/web/1/main/header", "web/1 header", {}),
    ("https://www.avito.ru/s/telefony?q=iphone", "s/ SPA", {}),
    # старый мобильный API (viktor-gorinskiy/avito)
    (f"https://m.avito.ru/api/9/items?key={MOBILE_KEY}&categoryId=84&locationId=621540&limit=5",
     "mobile api/9 items", {}),
]

s = curl_requests.Session(impersonate="safari15_5")
s.headers.update({"accept": "text/html,application/json,*/*;q=0.8", "accept-language": "ru-RU,ru;q=0.9"})

for url, label, _ in targets:
    try:
        r = s.get(url, timeout=20, allow_redirects=False)
        body = r.content or b""
        ct = r.headers.get("content-type", "")[:40]
        head = body[:120].decode("utf-8", "replace").replace("\n", " ")
        print(f"[{label}] {r.status_code} bytes={len(body)} ct={ct}")
        print(f"  head: {head}")
    except Exception as e:
        print(f"[{label}] ERR {type(e).__name__}: {str(e)[:100]}")
    time.sleep(2.5)
