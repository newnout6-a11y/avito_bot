# language: Python, file: research/probe_new_vs_bumped.py
# Проверка калитки "только новые": отличает ли монитор реально новое от поднятого.
#
# Вывод (живой замер 02.09.2026, telefony <70k, s=104):
#   Avito при поднятии/перепубликации ставит sortTimeStamp И allowTimeStamp в now,
#   а iva.DateInfoStep пишет "1 час назад" даже для объявления 5-недельной давности.
#   По времени поднятое старьё неотличимо от нового. Отличает только item-ID:
#   он монотонен по времени создания (замер research: ~6M ID/сутки).
#
# Скрипт делает 2 опроса, для новых между ними id сравнивает со старой калиткой
# (только время) и новой (id > фронтира). Запуск из корня:
#   PYTHONPATH=. python research/probe_new_vs_bumped.py
import sys
import time
from datetime import datetime

from bs4 import BeautifulSoup

import avito_api
from avito_api import AvitoHttpClient, _embedded_catalog, classify_block

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PUBLIC_URL = (
    "https://www.avito.ru/all/telefony/mobile-ASgBAgICAUSwwQ2I_Dc"
    "?context=H4sIAAAAAAAA_wEmANn_YToxOntzOjE6InkiO3M6MTY6IllkRGY2dmVrWDlzbnhLV0MiO30OA_gXJgAAAA"
    "&f=ASgBAgECAUSwwQ2I_DcBRcaaDBV7ImZyb20iOjAsInRvIjo3MDAwMH0&s=104"
)
GAP_SEC = 90


def dts(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "—"


def fetch(client):
    for _ in range(3):
        r = client.session.get(PUBLIC_URL, timeout=30, allow_redirects=True,
                               headers={"referer": "https://www.avito.ru/"})
        block = classify_block(r.status_code, r.headers, r.text or "")
        if block and block.kind == "challenge":
            avito_api.solve_pow_challenge(client.session, r)
            time.sleep(1)
            continue
        if block:
            print(f"  БЛОК {block.kind} http{block.status}")
            return {}
        if r.status_code == 200:
            cat = _embedded_catalog(BeautifulSoup(r.text, "html.parser")) or {}
            return {str(it["id"]): it for it in cat.get("items", [])
                    if isinstance(it, dict) and it.get("id") and it.get("title")}
        time.sleep(2)
    return {}


def num(i):
    try:
        return int(i)
    except (TypeError, ValueError):
        return 0


def main():
    client = AvitoHttpClient(timeout=30)
    client.warmup()

    p1 = fetch(client)
    if not p1:
        return client.close()
    frontier = max(num(i) for i in p1)          # как _prime_seen: фронтир = max id
    print(f"POLL 1: {len(p1)} объявлений, фронтир свежести id={frontier}")

    time.sleep(GAP_SEC)
    p2 = fetch(client)
    if not p2:
        return client.close()
    now = time.time()
    new_ids = [i for i in p2 if i not in p1]
    print(f"POLL 2: {len(p2)} объявлений, новых id: {len(new_ids)}\n")

    old_gate, new_gate = [], []
    for i in new_ids:
        it = p2[i]
        sts = avito_api._timestamp_seconds(it.get("sortTimeStamp"))
        ats = avito_api._timestamp_seconds(it.get("allowTimeStamp"))
        fresh_by_time = sts is not None and sts + 10 >= (now - GAP_SEC - 600)
        fresh_by_id = num(i) > frontier
        if fresh_by_time:
            old_gate.append(i)
        if fresh_by_id:
            new_gate.append(i)
        tag = "  НОВОЕ" if fresh_by_id else "  <-- поднятое старое (id ниже фронтира)"
        print(f"  id={i}  sortTS={dts(sts)} allowTS={dts(ats)}  Δid={num(i)-frontier:+d}{tag}")

    print(f"\nстарая калитка (по времени): доставила бы {len(old_gate)} из {len(new_ids)}")
    print(f"новая калитка (id > фронтира): доставила бы {len(new_gate)} из {len(new_ids)}")
    print(f"отсеяно поднятого старья: {len(old_gate) - len(new_gate)}")
    client.close()


if __name__ == "__main__":
    main()
