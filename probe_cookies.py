# language: Python, file: probe_cookies.py
# Live probe: полная карта cookies Avito после home -> PoW solve -> items.
# Цель: понять, какие куки формируют "репутацию сессии" и что ставит firewall.
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
    "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
})

report = {"steps": []}


def snap(label, response=None):
    cookies = {}
    for c in s.cookies.jar:
        cookies[c.name] = {
            "value_len": len(c.value or ""),
            "domain": c.domain,
            "path": c.path,
            "expires": c.expires,
        }
    step = {"label": label, "cookies": sorted(cookies.keys())}
    if response is not None:
        step["status"] = response.status_code
        sc = response.headers.get("set-cookie", "")
        step["set_cookie_names"] = [x.split("=")[0].strip() for x in sc.split(",") if "=" in x]
    report["steps"].append(step)
    report.setdefault("cookie_detail", {}).update(cookies)
    print(f"[{label}] status={step.get('status', '-')} cookies={len(cookies)}")
    for name in sorted(cookies):
        d = cookies[name]
        exp = d["expires"]
        exp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(exp)) if exp and exp > 0 else "session"
        print(f"  {name:20s} len={d['value_len']:5d} expires={exp_str}")
    if response is not None and step.get("set_cookie_names"):
        print(f"  set-cookie this step: {step['set_cookie_names']}")


# Step 1: home
r1 = s.get(HOME, timeout=25, allow_redirects=False)
snap("home", r1)

time.sleep(2)

# Step 2: API call — ждём challenge
r2 = s.get(API_URL, timeout=25, allow_redirects=False,
           headers={"referer": HOME, "accept": "application/json, text/plain, */*",
                    "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin"})
snap("api_first", r2)
print(f"  body[:200]: {(r2.text or '')[:200]!r}")

if r2.status_code in (403, 429, 439):
    time.sleep(1.5)
    ok = solve_pow_challenge(s, r2)
    snap("after_pow_solve")
    print(f"  pow solved: {ok}")

    time.sleep(1.5)
    r3 = s.get(API_URL, timeout=25, allow_redirects=False,
               headers={"referer": HOME, "accept": "application/json, text/plain, */*",
                        "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin"})
    snap("api_retry", r3)
    if r3.status_code == 200:
        try:
            data = r3.json()
            items = data.get("result", {}).get("items", [])
            print(f"  ITEMS: {len(items)}")
        except Exception as e:
            print(f"  json err: {e}")
    # Второй запрос подряд — держится ли сессия
    time.sleep(3)
    r4 = s.get(API_URL, timeout=25, allow_redirects=False,
               headers={"referer": HOME, "accept": "application/json, text/plain, */*",
                        "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin"})
    snap("api_immediately_again", r4)
    print(f"  second-call status={r4.status_code}, len={len(r4.content or b'')}")

# Step 3: другой путь — /s/ страница поиска HTML (не API)
time.sleep(3)
r5 = s.get("https://www.avito.ru/moskva/telefony?q=iphone", timeout=25, allow_redirects=False,
           headers={"referer": HOME})
snap("search_page_html", r5)
print(f"  html len={len(r5.content or b'')}")

with open("probe_cookies.json", "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
print("\nsaved probe_cookies.json")
