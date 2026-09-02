# language: Python, file: probe_free_proxy_avito_check.py
# Сборка мини-пула из бесплатных RU-источников + живая проверка каждого
# против avito.ru. Ответ на вопрос: пропускает ли Qrator бесплатные RU IP.
import json
import sys
import time
import urllib.request

from curl_cffi import requests as curl_requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 1. Собираем кандидатов: geonode RU (json) + proxyscrape RU (txt)
candidates = []

try:
    req = urllib.request.Request(
        "https://proxylist.geonode.com/api/proxy-list?limit=50&page=1&country=RU&protocols=http",
        headers={"user-agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read().decode())
    for row in data.get("data", []):
        candidates.append({
            "src": "geonode",
            "url": f"http://{row['ip']}:{row['port']}",
            "asn": row.get("asn"),
            "anonymity": row.get("anonymityLevel"),
            "org": (row.get("org") or "")[:30],
        })
except Exception as e:
    print(f"geonode: ERR {e}")

try:
    req = urllib.request.Request(
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=RU",
        headers={"user-agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        for line in resp.read().decode().splitlines():
            if line.strip():
                candidates.append({"src": "proxyscrape", "url": f"http://{line.strip()}",
                                   "asn": None, "anonymity": None, "org": None})
except Exception as e:
    print(f"proxyscrape: ERR {e}")

print(f"кандидатов: {len(candidates)} (geonode={sum(1 for c in candidates if c['src']=='geonode')}, "
      f"proxyscrape={sum(1 for c in candidates if c['src']=='proxyscrape')})")

# 2. Проверка: доступность + ответ Avito через прокси
results = []
for i, cand in enumerate(candidates):
    out = dict(cand)
    t0 = time.time()
    try:
        s = curl_requests.Session(impersonate="safari15_5",
                                  proxies={"http": cand["url"], "https": cand["url"]})
        r = s.get("https://www.avito.ru/", timeout=8, allow_redirects=False)
        out["connect"] = True
        out["status"] = r.status_code
        out["ms"] = int((time.time() - t0) * 1000)
        out["bytes"] = len(r.content or b"")
    except Exception as e:
        out["connect"] = False
        out["status"] = None
        out["err"] = f"{type(e).__name__}: {str(e)[:60]}"
        out["ms"] = int((time.time() - t0) * 1000)
    results.append(out)
    mark = "OK " if out.get("status") == 200 else "   "
    print(f"[{i + 1:2d}/{len(candidates)}] {mark} {cand['url']:28s} -> "
          f"{out.get('status')} {out['ms']}ms {out.get('err', '')[:40]}")

ok = [r for r in results if r.get("status") == 200]
connected = [r for r in results if r.get("connect")]
statuses = {}
for r in results:
    if r.get("status") is not None:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1

print("\n=== ИТОГ ===")
print(f"всего: {len(results)} | connect ok: {len(connected)} | avito 200: {len(ok)}")
print(f"статусы Avito: {statuses}")
if ok:
    print("проходят:")
    for r in ok:
        print(f"  {r['url']} {r['ms']}ms {r.get('org', '')}")
with open("probe_free_proxy_avito_check.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("saved probe_free_proxy_avito_check.json")
