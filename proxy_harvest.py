# -*- coding: utf-8 -*-
"""
proxy_harvest — конвейер «как ищут прокси» в одной утилите.

Забирает кандидатов из открытых источников (geonode JSON API — основной,
ProxyScrape и proxifly RU-шарды — дополнительные), проверяет каждый через
curl_cffi против цели и экспортирует живые в формате AVITO_PROXIES.

Живой замер 27.08.2026 (66 бесплатных RU кандидатов -> avito.ru):
соединились 6%, прошли 3% (оба — датацентр-ASN, 4-5 c). Для Avito это
аварийный пул последней надежды; для незащищённых целей — рабочий конвейер.

Использование:
    python proxy_harvest.py                # собрать + проверить + отчёт
    python proxy_harvest.py --export env   # + строка для AVITO_PROXIES
"""

import argparse
import json
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from curl_cffi import requests as curl_requests

GEONODE_RU = (
    "https://proxylist.geonode.com/api/proxy-list"
    "?limit=100&page=1&country=RU&protocols=http&sort_by=lastChecked&sort_type=desc"
)
PROXYSCRAPE_RU = (
    "https://api.proxyscrape.com/v2/?request=displayproxies"
    "&protocol=http&timeout=5000&country=RU"
)
PROXIFLY_RU = (
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main"
    "/proxies/countries/RU/data.txt"
)

DEFAULT_TARGET = "https://www.avito.ru/"
CHECK_TIMEOUT = 8.0


@dataclass
class ProxyCandidate:
    url: str
    source: str
    asn: Optional[str] = None
    org: Optional[str] = None
    anonymity: Optional[str] = None


@dataclass
class HarvestReport:
    target: str
    harvested: int = 0
    by_source: Dict[str, int] = field(default_factory=dict)
    connected: int = 0
    passed: int = 0
    statuses: Dict[int, int] = field(default_factory=dict)
    working: List[Dict[str, object]] = field(default_factory=list)

    def summary(self) -> str:
        src = ", ".join(f"{k}={v}" for k, v in sorted(self.by_source.items()))
        return (
            f"собрано {self.harvested} ({src}); соединились {self.connected}; "
            f"цель дала 200: {self.passed}; статусы {self.statuses or '{}'}"
        )


def _fetch_text(url: str, timeout: float = 25.0) -> str:
    req = urllib.request.Request(url, headers={"user-agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def harvest_geonode() -> List[ProxyCandidate]:
    out: List[ProxyCandidate] = []
    try:
        data = json.loads(_fetch_text(GEONODE_RU))
        for row in data.get("data", []):
            out.append(ProxyCandidate(
                url=f"http://{row['ip']}:{row['port']}",
                source="geonode",
                asn=row.get("asn"),
                org=(row.get("org") or "")[:40],
                anonymity=row.get("anonymityLevel"),
            ))
    except Exception:
        pass
    return out


def harvest_txt(url: str, source: str) -> List[ProxyCandidate]:
    out: List[ProxyCandidate] = []
    try:
        for line in _fetch_text(url).splitlines():
            line = line.strip()
            if line and ":" in line:
                addr = line if "://" in line else f"http://{line}"
                out.append(ProxyCandidate(url=addr, source=source))
    except Exception:
        pass
    return out


def harvest() -> List[ProxyCandidate]:
    """Все кандидаты из всех источников (дедуп по url)."""
    candidates = (
        harvest_geonode()
        + harvest_txt(PROXYSCRAPE_RU, "proxyscrape")
        + harvest_txt(PROXIFLY_RU, "proxifly")
    )
    seen: set = set()
    unique: List[ProxyCandidate] = []
    for c in candidates:
        if c.url not in seen:
            seen.add(c.url)
            unique.append(c)
    return unique


def check_proxy(candidate: ProxyCandidate, target: str = DEFAULT_TARGET,
                timeout: float = CHECK_TIMEOUT) -> Dict[str, object]:
    """Проверка одного прокси: connect + статус цели + latency."""
    result: Dict[str, object] = {
        "url": candidate.url, "source": candidate.source,
        "asn": candidate.asn, "org": candidate.org,
        "anonymity": candidate.anonymity,
    }
    t0 = time.time()
    try:
        session = curl_requests.Session(
            impersonate="safari15_5",
            proxies={"http": candidate.url, "https": candidate.url},
        )
        response = session.get(target, timeout=timeout, allow_redirects=False)
        result["connect"] = True
        result["status"] = response.status_code
        result["bytes"] = len(response.content or b"")
    except Exception as exc:
        result["connect"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"[:80]
    result["ms"] = int((time.time() - t0) * 1000)
    return result


def run_check(target: str = DEFAULT_TARGET,
              candidates: Optional[List[ProxyCandidate]] = None) -> HarvestReport:
    """Полный цикл: сбор -> проверка -> отчёт."""
    if candidates is None:
        candidates = harvest()
    report = HarvestReport(target=target)
    report.harvested = len(candidates)
    for c in candidates:
        report.by_source[c.source] = report.by_source.get(c.source, 0) + 1
        result = check_proxy(c, target)
        if result.get("connect"):
            report.connected += 1
            status = result.get("status")
            if isinstance(status, int):
                report.statuses[status] = report.statuses.get(status, 0) + 1
            if status == 200:
                report.passed += 1
                report.working.append(result)
    report.working.sort(key=lambda r: r.get("ms", 999999))
    return report


def export_env_string(report: HarvestReport) -> str:
    """Строка для AVITO_PROXIES из прошедших проверку."""
    return ",".join(str(r["url"]) for r in report.working)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--export", choices=["env"], help="вывести строку AVITO_PROXIES")
    parser.add_argument("--json-out", default="proxy_harvest_report.json")
    args = parser.parse_args()

    report = run_check(args.target)
    print(report.summary())
    for r in report.working:
        print(f"  OK {r['url']} {r['ms']}ms {r.get('org') or ''}")
    if args.export == "env":
        print(f"\nAVITO_PROXIES={export_env_string(report)}")
    try:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(report.__dict__, f, ensure_ascii=False, indent=2)
        print(f"отчёт: {args.json_out}")
    except Exception:
        pass
    return 0 if report.harvested else 1


if __name__ == "__main__":
    sys.exit(main())
