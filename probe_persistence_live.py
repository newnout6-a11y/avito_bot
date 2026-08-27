# language: Python, file: probe_persistence_live.py
# Автономная сквозная проверка: ждёт разбана IP, затем проверяет полную цепочку
# cookie persistence — клиент 1 прогревается и сохраняет куки, клиент 2
# (полностью новый, как после рестарта бота) подхватывает куки и берёт API
# без повторного прогрева. Результат пишет в probe_persistence_live.json.
import json
import sys
import time

from curl_cffi import requests as curl_requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API_URL = (
    "https://www.avito.ru/web/1/js/items?categoryId=84&localPriority=0"
    "&locationId=621540&presentationType=serp&query=samsung&sort=date"
)
HOME = "https://www.avito.ru/"
STORE = "data/cookies_probe_live.json"
RESULT_FILE = "probe_persistence_live.json"
MAX_WAIT_MIN = 8 * 60  # до 8 часов ожидания

report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "phases": []}


def phase(name, **kv):
    kv["t"] = time.strftime("%H:%M:%S")
    report["phases"].append({"name": name, **kv})
    print(f"[{kv['t']}] {name}: {kv}")
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def ban_status():
    """Лёгкий опрос: 200 = разбан, 403/429 = бан."""
    try:
        s = curl_requests.Session(impersonate="safari15_5")
        s.headers.update({"accept": "text/html,*/*;q=0.8", "accept-language": "ru-RU,ru;q=0.9"})
        r = s.get(HOME, timeout=25, allow_redirects=False)
        return r.status_code
    except Exception as e:
        return f"ERR:{type(e).__name__}"


# Фаза 0: ждём разбана (проверка раз в 10 минут, не дёргаем лишний раз)
phase("wait_start", note="проверка каждые 10 мин, до 8 ч")
deadline = time.time() + MAX_WAIT_MIN * 60
code = None
while time.time() < deadline:
    code = ban_status()
    if code == 200:
        phase("unbanned", code=code)
        break
    phase("still_banned", code=code)
    time.sleep(600)

if code != 200:
    phase("give_up", code=code, note="8 ч истекли, бан держится")
    sys.exit(0)

time.sleep(30)  # пауза после разбана — не бросаемся сразу

# Фаза 1: клиент 1 — прогрев + PoW + 200 + сохранение кук
sys.path.insert(0, ".")
from avito_api import AvitoHttpClient  # noqa: E402

c1 = AvitoHttpClient(timeout=25, cookie_store=STORE)
try:
    c1.warmup()
    phase("c1_warmup", status=c1.last_status)
    time.sleep(3)
    data = c1.get_items(API_URL)
    items = data.get("catalog", {}).get("items", [])
    phase("c1_get_items", status=c1.last_status, items=len(items), pow=getattr(c1, "total_pow_solved", 0))
except Exception as e:
    phase("c1_error", error=f"{type(e).__name__}: {e}")
    sys.exit(0)
finally:
    c1.close()

time.sleep(20)

# Фаза 2: клиент 2 — полностью новый процесс-эквивалент рестарта бота
c2 = AvitoHttpClient(timeout=25, cookie_store=STORE)
try:
    phase("c2_cookies_loaded", count=c2.cookies_loaded)
    # warmup должен пройти без единого сетевого запроса (куки восстановлены)
    t0 = time.time()
    warm = c2.warmup()
    phase("c2_warmup_skipped", warm=warm, ms=int((time.time() - t0) * 1000))
    data2 = c2.get_items(API_URL)
    items2 = data2.get("catalog", {}).get("items", [])
    phase("c2_get_items", status=c2.last_status, items=len(items2), pow=getattr(c2, "total_pow_solved", 0),
          note="новый клиент с перенесёнными куками")
except Exception as e:
    phase("c2_error", error=f"{type(e).__name__}: {e}")
finally:
    c2.close()

ok = report["phases"][-1].get("items", 0) > 0 if report["phases"] else False
phase("VERDICT", persistence_works=bool(ok))
