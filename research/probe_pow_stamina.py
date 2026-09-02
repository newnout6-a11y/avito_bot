# language: Python, file: probe_pow_stamina.py
# Выносливость: серия запросов, каждый 439 решаем PoW на лету. Сколько продержимся.
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

N = 8
GAP = 12.0

if __name__ == "__main__":
    results = []
    s = curl_requests.Session(impersonate="firefox147")
    s.headers.update(BROWSER_HEADERS)
    r = s.get(HOME, timeout=25)
    print(json.dumps({"warmup": r.status_code, "cookies": sorted(dict(s.cookies).keys())}))
    for i in range(N):
        out = {"i": i}
        r = s.get(API_URL, timeout=25, headers=API_HEADERS)
        out["api"] = r.status_code
        if r.status_code in (429, 439):
            t0 = time.time()
            ok = solve_pow_challenge(s, r)
            out["pow"] = ok
            out["pow_sec"] = round(time.time() - t0, 2)
            if ok:
                time.sleep(1)
                r = s.get(API_URL, timeout=25, headers=API_HEADERS)
                out["api_retry"] = r.status_code
        if (out.get("api") == 200) or (out.get("api_retry") == 200):
            try:
                out["items"] = len(r.json().get("catalog", {}).get("items", []))
            except Exception as exc:
                out["items"] = f"json error: {exc}"
        results.append(out)
        print(json.dumps(out, ensure_ascii=False))
        time.sleep(GAP)
    with open("probe_pow_stamina.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    ok_n = sum(1 for x in results if x.get("api") == 200 or x.get("api_retry") == 200)
    pow_n = sum(1 for x in results if x.get("pow"))
    print(f"SUMMARY: {ok_n}/{N} успешных, {pow_n} PoW решено")
