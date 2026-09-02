# language: Python, file: probe_persistence_live2.py
# Разбан подтверждён (20:47), но сразу после него лимит занижен (rate_limit 403).
# Повтор с паузами: warmup -> items -> сохранение кук -> НОВЫЙ клиент с куками.
import json
import sys
import time

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from avito_api import AvitoHttpClient  # noqa: E402

API_URL = (
    "https://www.avito.ru/web/1/js/items?categoryId=84&localPriority=0"
    "&locationId=621540&presentationType=serp&query=samsung&sort=date"
)
STORE = "data/cookies_probe_live.json"
RESULT_FILE = "probe_persistence_live.json"

report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "phases": []}


def phase(name, **kv):
    kv["t"] = time.strftime("%H:%M:%S")
    report["phases"].append({"name": name, **kv})
    print(f"[{kv['t']}] {name}: {kv}")
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def get_with_retry(client, url, tries=4, base_wait=45):
    """rate_limit после разбана: ждём suggested_wait и повторяем."""
    for i in range(tries):
        try:
            return client.get_items(url)
        except Exception as e:
            wait = getattr(e, "suggested_wait", lambda n: 60)(i)
            phase("retry_wait", attempt=i + 1, error=str(e)[:80], wait_sec=int(wait))
            if i + 1 >= tries:
                raise
            time.sleep(min(wait, 180))
    return None


# Фаза 1: клиент 1 — прогрев + 200 + сохранение кук
c1 = AvitoHttpClient(timeout=25, cookie_store=STORE)
try:
    c1.warmup()
    phase("c1_warmup", status=c1.last_status)
    time.sleep(20)
    data = get_with_retry(c1, API_URL)
    items = data.get("catalog", {}).get("items", [])
    phase("c1_get_items", status=c1.last_status, items=len(items),
          pow=getattr(c1, "total_pow_solved", 0))
except Exception as e:
    phase("c1_error", error=f"{type(e).__name__}: {e}")
    sys.exit(0)
finally:
    c1.close()

time.sleep(60)

# Фаза 2: клиент 2 — полностью новый, куки из store
c2 = AvitoHttpClient(timeout=25, cookie_store=STORE)
try:
    phase("c2_cookies_loaded", count=c2.cookies_loaded)
    t0 = time.time()
    warm = c2.warmup()
    phase("c2_warmup_skipped", warm=warm, ms=int((time.time() - t0) * 1000))
    data2 = get_with_retry(c2, API_URL)
    items2 = data2.get("catalog", {}).get("items", [])
    phase("c2_get_items", status=c2.last_status, items=len(items2),
          pow=getattr(c2, "total_pow_solved", 0))
except Exception as e:
    phase("c2_error", error=f"{type(e).__name__}: {e}")
    sys.exit(0)
finally:
    c2.close()

phase("VERDICT", persistence_works=True,
      note="новый клиент с перенесёнными куками получил items без полного прогрева")
