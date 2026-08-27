# language: Python, file: probe_fingerprints.py
# Live probe: which curl_cffi impersonate targets pass Avito's Qrator edge today.
import json
import time

from curl_cffi import requests as curl_requests

API_URL = (
    "https://www.avito.ru/web/1/js/items?categoryId=84&localPriority=0"
    "&locationId=621540&presentationType=serp&query=samsung&sort=date"
)
HOME = "https://www.avito.ru/"
GAP = 20.0  # пауза между отпечатками, чтобы не спалить IP

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


def probe(imp: str) -> dict:
    out = {"impersonate": imp}
    try:
        s = curl_requests.Session(impersonate=imp)
        s.headers.update(BROWSER_HEADERS)
        t0 = time.time()
        r = s.get(HOME, timeout=25, allow_redirects=False)
        out["home_status"] = r.status_code
        out["home_ms"] = int((time.time() - t0) * 1000)
        out["home_bytes"] = len(r.content or b"")
        cookies = dict(s.cookies)
        out["cookies"] = list(cookies.keys())
        out["qrator_jsid"] = "qrator_jsid" in cookies
        if r.status_code == 200:
            time.sleep(3)
            t0 = time.time()
            r2 = s.get(API_URL, timeout=25, headers=API_HEADERS, allow_redirects=False)
            out["api_status"] = r2.status_code
            out["api_ms"] = int((time.time() - t0) * 1000)
            out["api_bytes"] = len(r2.content or b"")
            if r2.status_code == 200:
                try:
                    payload = r2.json()
                    items = payload.get("catalog", {}).get("items", [])
                    out["items"] = len(items)
                except Exception as exc:
                    out["items"] = f"json error: {exc}"
        s.close()
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


if __name__ == "__main__":
    results = []
    for imp in ["safari15_5", "chrome146", "chrome131_android", "firefox147"]:
        res = probe(imp)
        results.append(res)
        print(json.dumps(res, ensure_ascii=False))
        time.sleep(GAP)
    with open("probe_results.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
