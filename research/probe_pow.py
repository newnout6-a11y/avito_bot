# language: Python, file: probe_pow.py
# Живой тест: ловим 439, решаем PoW, перезапрашиваем — API должен стать 200.
import json
import time

from curl_cffi import requests as curl_requests

from avito_pow import solve_pow_challenge

API_URL = (
    "https://www.avito.ru/web/1/js/items?categoryId=84&localPriority=0"
    "&locationId=621540&presentationType=serp&query=samsung&sort=date"
)
HOME = "https://www.avito.ru/"

BROWSER_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "ru-RU,ru;q=0.9",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
}
API_HEADERS = {
    "referer": HOME,
    "accept": "application/json, text/plain, */*",
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
}


def attempt(imp: str) -> dict:
    out = {"impersonate": imp}
    s = curl_requests.Session(impersonate=imp)
    s.headers.update(BROWSER_HEADERS)
    r = s.get(HOME, timeout=25)
    out["home"] = r.status_code
    r = s.get(API_URL, timeout=25, headers=API_HEADERS)
    out["api_1st"] = r.status_code
    if r.status_code in (429, 439):
        t0 = time.time()
        ok = solve_pow_challenge(s, r)
        out["pow_solved"] = ok
        out["pow_sec"] = round(time.time() - t0, 2)
        if ok:
            time.sleep(1)
            r = s.get(API_URL, timeout=25, headers=API_HEADERS)
            out["api_retry"] = r.status_code
            if r.status_code == 200:
                try:
                    out["items"] = len(r.json().get("catalog", {}).get("items", []))
                except Exception as exc:
                    out["items"] = f"json error: {exc}"
    elif r.status_code == 200:
        try:
            out["items"] = len(r.json().get("catalog", {}).get("items", []))
        except Exception as exc:
            out["items"] = f"json error: {exc}"
    return out


if __name__ == "__main__":
    results = []
    for imp in ["firefox147", "safari15_5", "chrome146"]:
        res = attempt(imp)
        results.append(res)
        print(json.dumps(res, ensure_ascii=False))
        time.sleep(10)
    with open("probe_pow.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
