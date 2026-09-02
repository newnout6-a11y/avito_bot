# language: Python, file: probe_free_proxy_sources.py
# Живая промерка известных бесплатных прокси-листов: доступность, формат,
# сколько там RU. Данные сольются с отчётами субагентов.
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SOURCES = [
    ("geonode http", "https://proxylist.geonode.com/api/proxy-list?limit=50&page=1&country=RU&protocols=http"),
    ("monosans http", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("monosans socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
    ("TheSpeedX http", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    ("TheSpeedX socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    ("proxyscrape http", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=RU"),
    ("proxyscrape all", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000"),
    ("openproxylist http", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"),
]

for label, url in SOURCES:
    try:
        req = urllib.request.Request(url, headers={"user-agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            text = resp.read().decode("utf-8", "replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        # geonode отдаёт JSON: посчитаем ip:port внутри
        ru_marker = text.count('"RU"') + text.count('"ru"')
        print(f"[{label}] OK lines={len(lines)} chars={len(text)} ru_markers={ru_marker}")
        print(f"   head: {lines[0][:100] if lines else '-'}")
    except Exception as e:
        print(f"[{label}] ERR {type(e).__name__}: {str(e)[:100]}")
