# language: Python, file: probe_json_shape.py
# Найти, где в ответе /web/1/js/items лежат сами объявления.
import sys
import time

from curl_cffi import requests as curl_requests

import avito_api
from avito_pow import solve_pow_challenge

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API_URL = (
    "https://www.avito.ru/web/1/js/items?categoryId=84&localPriority=0"
    "&locationId=621540&presentationType=serp&query=samsung&sort=date"
)
HOME = "https://www.avito.ru/"

s = curl_requests.Session(impersonate="safari15_5")
s.headers.update({"accept": "text/html,*/*;q=0.8", "accept-language": "ru-RU,ru;q=0.9"})
s.get(HOME, timeout=25)

API_HEADERS = {
    "referer": HOME, "accept": "application/json, text/plain, */*",
    "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
}

r = None
for _ in range(3):
    time.sleep(2)
    r = s.get(API_URL, timeout=30, headers=API_HEADERS)
    if r.status_code == 439:
        time.sleep(1)
        solve_pow_challenge(s, r)
        continue
    break

print(f"status={r.status_code} bytes={len(r.content or b'')}")
data = r.json()

# Рекурсивный поиск массивов, похожих на объявления
def find_item_arrays(obj, path="$", depth=0, out=None):
    if out is None:
        out = []
    if depth > 6:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            find_item_arrays(v, f"{path}.{k}", depth + 1, out)
    elif isinstance(obj, list) and len(obj) >= 5:
        first = obj[0]
        if isinstance(first, dict) and any(
            key in first for key in ("title", "priceDetailed", "urlPath", "id", "images")
        ):
            out.append((path, len(obj), sorted(first.keys())[:12]))
    return out

arrays = find_item_arrays(data)
for path, count, keys in arrays:
    print(f"\nPATH: {path}  count={count}")
    print(f"  keys: {keys}")

# Как наш парсер читает этот ответ
items = avito_api.parse_api_items(data)
print(f"\nparse_api_items -> {len(items)} items")
if items:
    print(f"  first: {items[0].get('title', '?')[:60]}")
