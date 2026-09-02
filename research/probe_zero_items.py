# language: Python, file: probe_zero_items.py
# Почему первый retry после PoW даёт 200 + 0 items, а второй — 1.7MB данных?
# Гипотеза: Avito отдаёт "пустой" JSON первый раз (soft-check), полный — со второго.
import json
import time

from curl_cffi import requests as curl_requests

from avito_pow import solve_pow_challenge

API_URL = (
    "https://www.avito.ru/web/1/js/items?categoryId=84&localPriority=0"
    "&locationId=621540&presentationType=serp&query=samsung&sort=date"
)
HOME = "https://www.avito.ru/"

s = curl_requests.Session(impersonate="safari15_5")
s.headers.update({
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "ru-RU,ru;q=0.9",
})
s.get(HOME, timeout=25)

API_HEADERS = {
    "referer": HOME,
    "accept": "application/json, text/plain, */*",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

for attempt in range(5):
    time.sleep(2)
    r = s.get(API_URL, timeout=25, headers=API_HEADERS)
    body = r.content or b""
    label = f"attempt_{attempt + 1}"
    if r.status_code == 439:
        print(f"[{label}] 439 -> solving PoW")
        time.sleep(1)
        solve_pow_challenge(s, r)
        continue
    info = {"status": r.status_code, "bytes": len(body)}
    try:
        data = r.json()
        result = data.get("result", {})
        items = result.get("items", [])
        info["items"] = len(items)
        info["top_keys"] = list(data.keys())[:6]
        info["result_keys"] = list(result.keys())[:10] if isinstance(result, dict) else str(type(result))
        # Если items пуст — что вместо них?
        if not items:
            info["snippet"] = json.dumps(data, ensure_ascii=False)[:600]
    except Exception as e:
        info["json_error"] = str(e)
        info["body_head"] = body[:200].decode("utf-8", "replace")
    print(f"[{label}] {json.dumps(info, ensure_ascii=False)[:800]}")

print("done")
