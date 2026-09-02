# language: Python, file: probe_psi_proxy.py
# Гипотеза (с форума searchengines.guru, -= Serafim =-): поисковые боты у Avito
# в whitelist. PageSpeed Insights API заставляет GOOGLE самого зайти на URL
# и вернуть контент — живой прокси через инфраструктуру Google, бесплатно.
import json
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# PSI работает без ключа с малыми лимитами; с ключом — 25k/день.
TARGET = "https://www.avito.ru/moskva/telefony?q=iphone"

url = (
    "https://www.googleapis.com/pagespeedonline/v5/runPagespeed?"
    + urllib.parse.urlencode({"url": TARGET, "strategy": "desktop"})
)
print(f"requesting PSI for {TARGET} ...")
t0 = time.time()
req = urllib.request.Request(url, headers={"accept": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    dt = time.time() - t0
    print(f"PSI responded in {dt:.1f}s")

    lr = data.get("lighthouseResult", {})
    # Что PSI отдаёт из контента:
    audits = lr.get("audits", {})
    print(f"top-level keys: {sorted(data.keys())}")
    print(f"lighthouse keys: {sorted(lr.keys())[:12]}")

    # final url + status
    print(f"finalUrl: {lr.get('finalDisplayedUrl', lr.get('finalUrl', '?'))}")
    print(f"runtimeError: {audits.get('errors-in-console', {}).get('details', {}).get('items', [])[:1]}")

    # Ищем HTML-контент в отчёте
    for key in ("final-screenshot", "full-page-screenshot"):
        audit = audits.get(key) or lr.get(key) or data.get(key)
        if audit:
            detail = audit.get("details", audit) if isinstance(audit, dict) else audit
            if isinstance(detail, dict) and "data" in detail:
                print(f"{key}: present, data len={len(str(detail['data']))}")

    # DOM-size audit — сколько nodes Google увидел на странице
    dom = audits.get("dom-size")
    if dom:
        items = dom.get("details", {}).get("items", [])
        print(f"dom-size: {dom.get('displayValue')} (statistic)")

    # Проверка: видит ли Google объявления (ищем в итоговых узлах)
    # mainthread-work-breakdown и т.п. не содержат DOM. Полный HTML PSI не отдаёт,
    # но отдаёт截图 и метрики узлов.
    extra = data.get("loadingExperience", {})
    print(f"loadingExperience metrics: {sorted(extra.get('metrics', {}).keys())}")
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8", "replace")[:400]
    print(f"HTTP {e.code}: {body}")
except Exception as e:
    print(f"ERR {type(e).__name__}: {e}")
