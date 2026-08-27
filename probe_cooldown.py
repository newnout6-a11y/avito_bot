# language: Python, file: probe_cooldown.py
# Одиночная проверка: остыл ли IP после 429-бана? Один запрос, никаких циклов.
import sys
import time

from curl_cffi import requests as curl_requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

s = curl_requests.Session(impersonate="safari15_5")
s.headers.update({"accept": "text/html,*/*;q=0.8", "accept-language": "ru-RU,ru;q=0.9"})
r = s.get("https://www.avito.ru/", timeout=25, allow_redirects=False)
print(f"home: {r.status_code} bytes={len(r.content or b'')}")

if r.status_code == 200:
    time.sleep(2)
    r2 = s.get(
        "https://www.avito.ru/web/1/js/items?categoryId=84&localPriority=0"
        "&locationId=621540&presentationType=serp&query=samsung&sort=date",
        timeout=30,
        headers={"referer": "https://www.avito.ru/", "accept": "application/json, text/plain, */*",
                 "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin"},
    )
    print(f"api: {r2.status_code} bytes={len(r2.content or b'')}")
    if r2.status_code == 200:
        items = r2.json().get("catalog", {}).get("items", [])
        print(f"items: {len(items)} — IP чист")
    elif r2.status_code == 439:
        print("439 challenge — PoW-путь сработает (это не IP-бан)")
    else:
        head = (r2.content or b"")[:150].decode("utf-8", "replace")
        print(f"head: {head}")
