# language: Python, file: probe_session_carry.py
# Ключевой эксперимент: сохранить cookies тёплой сессии -> перенести в НОВУЮ
# сессию (новый TLS-handshake, тот же IP) -> работает ли API без повторного challenge?
import json
import sys
import time

from curl_cffi import requests as curl_requests

from avito_pow import solve_pow_challenge

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
API_URL = (
    "https://www.avito.ru/web/1/js/items?categoryId=84&localPriority=0"
    "&locationId=621540&presentationType=serp&query=samsung&sort=date"
)
HOME = "https://www.avito.ru/"

API_HEADERS = {
    "referer": HOME, "accept": "application/json, text/plain, */*",
    "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
}

# --- Фаза 1: прогреть сессию 1 до полного 200 через PoW ---
s1 = curl_requests.Session(impersonate="safari15_5")
s1.headers.update({"accept": "text/html,*/*;q=0.8", "accept-language": "ru-RU,ru;q=0.9"})
r = s1.get(HOME, timeout=25)
print(f"[s1 home] {r.status_code}")
time.sleep(2)
for attempt in range(3):
    r = s1.get(API_URL, timeout=30, headers=API_HEADERS)
    print(f"[s1 api try{attempt + 1}] {r.status_code}")
    if r.status_code == 200:
        break
    if r.status_code in (403, 429, 439):
        time.sleep(1)
        ok = solve_pow_challenge(s1, r)
        print(f"[s1 pow] solved={ok}")
        time.sleep(1.5)

data = r.json()
n_items = len(data.get("catalog", {}).get("items", []))
print(f"[s1] warm session: {r.status_code}, catalog.items={n_items}")

# Снимок cookies
saved = []
for c in s1.cookies.jar:
    saved.append({
        "name": c.name, "value": c.value, "domain": c.domain,
        "path": c.path, "expires": c.expires,
    })
print(f"[s1] cookies captured: {len(saved)}")
with open("probe_session_carry.json", "w", encoding="utf-8") as f:
    json.dump(saved, f, indent=2)

# --- Фаза 2: НОВАЯ сессия с перенесёнными cookies, БЕЗ прогрева home ---
time.sleep(3)
s2 = curl_requests.Session(impersonate="safari15_5")
s2.headers.update({"accept": "text/html,*/*;q=0.8", "accept-language": "ru-RU,ru;q=0.9"})
for ck in saved:
    try:
        s2.cookies.set(ck["name"], ck["value"], domain=ck["domain"], path=ck["path"])
    except Exception as e:
        print(f"  set err {ck['name']}: {e}")

r2 = s2.get(API_URL, timeout=30, headers=API_HEADERS)
status2 = r2.status_code
items2 = 0
if status2 == 200:
    try:
        items2 = len(r2.json().get("catalog", {}).get("items", []))
    except Exception:
        pass
print(f"\n[s2 COLD session + carried cookies] api -> {status2}, items={items2}")
if status2 == 439:
    print("[s2] challenge пришёл и на новой сессии с куками — репутация не в cookies или требуется прогрев")
    body = (r2.content or b"")[:150].decode("utf-8", "replace")
    print(f"  body: {body}")
elif status2 == 200:
    print("[s2] ПОБЕДА: cookies переносят репутацию, прогрев не нужен")
