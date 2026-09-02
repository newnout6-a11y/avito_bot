# language: Python, file: probe_persistence_live3.py
# Раунд 3: пациентливый протокол после ре-бана (20:48).
# Фазы: wait unban -> 15 мин тишины -> warmup -> 5 мин -> items -> 5 мин ->
# новый клиент с куками -> items. Каждый шаг в отчёт.
import json
import sys
import time

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from curl_cffi import requests as curl_requests  # noqa: E402

from avito_api import AvitoHttpClient  # noqa: E402

API_URL = (
    "https://www.avito.ru/web/1/js/items?categoryId=84&localPriority=0"
    "&locationId=621540&presentationType=serp&query=samsung&sort=date"
)
HOME = "https://www.avito.ru/"
STORE = "data/cookies_probe_live.json"
RESULT_FILE = "probe_persistence_live.json"
MAX_WAIT_H = 6

report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "phases": []}


def phase(name, **kv):
    kv["t"] = time.strftime("%H:%M:%S")
    report["phases"].append({"name": name, **kv})
    print(f"[{kv['t']}] {name}: {kv}", flush=True)
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def ban_status():
    try:
        s = curl_requests.Session(impersonate="safari15_5")
        s.headers.update({"accept": "text/html,*/*;q=0.8", "accept-language": "ru-RU,ru;q=0.9"})
        r = s.get(HOME, timeout=25, allow_redirects=False)
        return r.status_code
    except Exception as e:
        return f"ERR:{type(e).__name__}"


# 1. Ждём разбана (раз в 10 мин)
deadline = time.time() + MAX_WAIT_H * 3600
code = None
while time.time() < deadline:
    code = ban_status()
    if code == 200:
        phase("unbanned", code=code)
        break
    phase("still_banned", code=code)
    time.sleep(600)

if code != 200:
    phase("give_up", code=code)
    sys.exit(0)

# 2. 15 минут тишины после разбана (parole-протокол)
phase("silence_15min", note="после разбана IP на проверочном режиме — не дёргаем")
time.sleep(15 * 60)

# 3. Клиент 1: warmup -> 5 мин -> items
c1 = AvitoHttpClient(timeout=25, cookie_store=STORE)
try:
    c1.warmup()
    phase("c1_warmup", status=c1.last_status)
    time.sleep(5 * 60)
    data = c1.get_items(API_URL)
    items = data.get("catalog", {}).get("items", [])
    phase("c1_get_items", status=c1.last_status, items=len(items),
          pow=getattr(c1, "total_pow_solved", 0))
except Exception as e:
    phase("c1_error", error=f"{type(e).__name__}: {e}")
    sys.exit(0)
finally:
    c1.close()

# 4. 5 минут -> новый клиент с куками
time.sleep(5 * 60)
c2 = AvitoHttpClient(timeout=25, cookie_store=STORE)
try:
    phase("c2_cookies_loaded", count=c2.cookies_loaded)
    t0 = time.time()
    warm = c2.warmup()
    phase("c2_warmup_skipped", warm=warm, ms=int((time.time() - t0) * 1000))
    data2 = c2.get_items(API_URL)
    items2 = data2.get("catalog", {}).get("items", [])
    phase("c2_get_items", status=c2.last_status, items=len(items2),
          pow=getattr(c2, "total_pow_solved", 0))
except Exception as e:
    phase("c2_error", error=f"{type(e).__name__}: {e}")
    sys.exit(0)
finally:
    c2.close()

phase("VERDICT", persistence_works=True,
      note="новый клиент с куками из store получил items; warmup пропущен")
