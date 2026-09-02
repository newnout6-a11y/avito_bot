# language: Python, file: probe_sitemap_items.py
# Распаковка .gz-саймапов вручную. Ищем item-URL (с числовым ID) и lastmod.
import gzip
import re
import sys
import time

from curl_cffi import requests as curl_requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

s = curl_requests.Session(impersonate="safari15_5")
s.headers.update({"accept": "*/*", "accept-language": "ru-RU,ru;q=0.9"})

# Телефоны: canonical_serp для категории telefony, если есть
r = s.get("https://www.avito.ru/sitemap/index.xml", timeout=30)
locs = re.findall(r"<loc>([^<]+)</loc>", r.text or "")

# Какие семейства вообще есть? Группируем по префиксу
families = {}
for loc in locs:
    name = loc.rsplit("/", 1)[-1].rsplit(".xml", 1)[0]
    fam = re.sub(r"[_0-9]+$", "", name)
    families.setdefault(fam, 0)
    families[fam] += 1
print("семейства sitemap:")
for fam, n in sorted(families.items()):
    print(f"  {fam}: {n}")

# Ищем telefony и любые с item-паттерном
telefony = [u for u in locs if "telefon" in u.lower()]
print(f"\ntelefony-саймапы: {len(telefony)}")
for t in telefony[:10]:
    print(f"  {t}")

# Качаем первый telefony, распаковываем
target = telefony[0] if telefony else locs[2]
time.sleep(2)
r2 = s.get(target, timeout=60)
print(f"\n{target}: {r2.status_code} raw={len(r2.content or b'')}")
if r2.status_code == 200:
    raw = r2.content or b""
    try:
        xml = gzip.decompress(raw).decode("utf-8", "replace")
    except Exception:
        xml = raw.decode("utf-8", "replace")
    print(f"unpacked: {len(xml)} chars")
    urls = re.findall(r"<loc>([^<]+)</loc>", xml)
    lastmods = re.findall(r"<lastmod>([^<]+)</lastmod>", xml)
    print(f"urls: {len(urls)}, lastmods: {len(lastmods)}")
    pairs = list(zip(urls, lastmods + ["?"] * len(urls)))
    for u, lm in pairs[:15]:
        print(f"  {lm}  {u[:110]}")
    # item-URL паттерн: ..._<числа> в конце
    item_urls = [u for u in urls if re.search(r"_\d+$", u.strip("/"))]
    print(f"\nпохожи на item-URL (числовой суффикс): {len(item_urls)}")
    for u in item_urls[:10]:
        print(f"  {u[:110]}")
