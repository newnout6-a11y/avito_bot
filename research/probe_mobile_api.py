# language: Python, file: probe_mobile_api.py
# Мобильный API m.avito.ru/api/* отвечает 400 — endpoint существует.
# Разбираем формат ошибки и подбираем параметры.
import sys
import time

from curl_cffi import requests as curl_requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
MOBILE_KEY = "af0deccbgcgidddjgnvljitntccdduijhdinfgjgfjir"

s = curl_requests.Session(impersonate="safari15_5")
s.headers.update({
    "accept": "application/json, text/plain, */*",
    "accept-language": "ru-RU,ru;q=0.9",
    "user-agent": "Avito/7.21.0 (Android 14; REDMI)",
})

# Шаг 1: что говорит 400
r = s.get(
    f"https://m.avito.ru/api/9/items?key={MOBILE_KEY}&categoryId=84&locationId=621540&limit=5",
    timeout=20,
)
print(f"api/9/items: {r.status_code}")
print(f"body: {r.content[:500].decode('utf-8', 'replace')}")

time.sleep(2)

# Шаг 2: пробы разных версий API и путей
probes = [
    "https://m.avito.ru/api/9/items",
    "https://m.avito.ru/api/10/items",
    "https://m.avito.ru/api/11/items",
    "https://m.avito.ru/api/1/items",
    "https://m.avito.ru/api/items",
]
for url in probes:
    try:
        r = s.get(f"{url}?key={MOBILE_KEY}&categoryId=84&locationId=621540&limit=5", timeout=20)
        body = r.content[:200].decode("utf-8", "replace")
        print(f"\n[{url.split('.ru')[1]}] {r.status_code}: {body}")
    except Exception as e:
        print(f"\n[{url.split('.ru')[1]}] ERR {type(e).__name__}")
    time.sleep(2)
