# language: Python, file: probe_stability.py
# Стабильность firefox147 на JSON API: серия запросов с паузами, трек статусов/кук.
import json
import time

from curl_cffi import requests as curl_requests

API_URL = (
    "https://www.avito.ru/web/1/js/items?categoryId=84&localPriority=0"
    "&locationId=621540&presentationType=serp&query=samsung&sort=date"
)
HOME = "https://www.avito.ru/"

BROWSER_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}

API_HEADERS = {
    "referer": HOME,
    "accept": "application/json, text/plain, */*",
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
}

N = 6
GAP = 15.0

if __name__ == "__main__":
    results = []
    s = curl_requests.Session(impersonate="firefox147")
    s.headers.update(BROWSER_HEADERS)
    r = s.get(HOME, timeout=25, allow_redirects=False)
    print(json.dumps({"warmup_status": r.status_code, "cookies": list(dict(s.cookies).keys())}))
    for i in range(N):
        out = {"i": i}
        t0 = time.time()
        try:
            r = s.get(API_URL, timeout=25, headers=API_HEADERS, allow_redirects=False)
            out["status"] = r.status_code
            out["ms"] = int((time.time() - t0) * 1000)
            out["bytes"] = len(r.content or b"")
            if r.status_code == 200:
                try:
                    payload = r.json()
                    out["items"] = len(payload.get("catalog", {}).get("items", []))
                except Exception as exc:
                    out["items"] = f"json error: {exc}"
            elif r.status_code in (403, 429, 439):
                snippet = (r.text or "")[:300]
                out["snippet"] = snippet
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
        results.append(out)
        print(json.dumps(out, ensure_ascii=False))
        time.sleep(GAP)
    with open("probe_stability.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
