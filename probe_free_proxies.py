# language: Python, file: probe_free_proxies.py
# Бесплатные "живые прокси" — сервисы, которые сами заходят на URL со своих IP:
# 1. r.jina.ai — reader, рендерит JS, бесплатный
# 2. allorigins / codetabs — CORS-прокси
# 3. Wayback availability — только архив (лаг), для контроля
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TARGET = "https://www.avito.ru/moskva/telefony?q=iphone"

probes = [
    ("jina reader", f"https://r.jina.ai/{TARGET}"),
    ("allorigins", f"https://api.allorigins.win/raw?url={TARGET}"),
    ("codetabs", f"https://api.codetabs.com/v1/proxy?quest={TARGET}"),
]

for label, url in probes:
    try:
        req = urllib.request.Request(url, headers={"accept": "text/html,*/*"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", "replace")
        marker = "iphone" in body.lower() or "объявлен" in body.lower()
        print(f"[{label}] {resp.status} bytes={len(body)} avito-content={marker}")
        # Ищем признаки объявлений
        for kw in ("iPhone", "₽", "title", "avito"):
            idx = body.lower().find(kw.lower())
            if idx >= 0:
                snippet = body[max(0, idx - 40):idx + 80].replace("\n", " ")
                print(f"   '{kw}' @ {idx}: ...{snippet}...")
                break
    except Exception as e:
        print(f"[{label}] ERR {type(e).__name__}: {str(e)[:120]}")
