# language: Python, file: proxy_sweep.py
# Массовая проверка большого прокси-листа (десятки тысяч) против Avito.
# Потоковый парсер + ThreadPoolExecutor. Сначала sample-оценка, потом полный зачист.
import argparse
import concurrent.futures as cf
import ipaddress
import json
import random
import re
import sys
import time

from curl_cffi import requests as curl_requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TARGET = "https://www.avito.ru/"
ADDR_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})$")


def parse_file(path: str) -> list[str]:
    """Валидные ip:port из файла (мусор/реклама/пустые строки отбрасываются)."""
    seen = set()
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = ADDR_RE.match(line.strip())
            if not m:
                continue
            ip, port = m.group(1), m.group(2)
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                continue
            if not (1 <= int(port) <= 65535):
                continue
            addr = f"{ip}:{port}"
            if addr not in seen:
                seen.add(addr)
                out.append(addr)
    return out


def check_one(addr: str, timeout: float = 6.0) -> dict:
    out = {"addr": addr}
    t0 = time.time()
    try:
        s = curl_requests.Session(
            impersonate="safari15_5",
            proxies={"http": f"http://{addr}", "https": f"http://{addr}"},
        )
        r = s.get(TARGET, timeout=timeout, allow_redirects=False)
        out["status"] = r.status_code
        out["bytes"] = len(r.content or b"")
    except Exception as e:
        out["status"] = None
        out["err"] = type(e).__name__
    out["ms"] = int((time.time() - t0) * 1000)
    return out


def sweep(addrs: list[str], workers: int, label: str) -> list[dict]:
    results = []
    done_count = 0
    statuses: dict = {}
    t_start = time.time()
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check_one, a): a for a in addrs}
        for fut in cf.as_completed(futures):
            r = fut.result()
            results.append(r)
            done_count += 1
            st = r.get("status")
            statuses[st] = statuses.get(st, 0) + 1
            if done_count % 500 == 0:
                elapsed = time.time() - t_start
                rate = done_count / elapsed
                eta = (len(addrs) - done_count) / rate if rate else 0
                ok200 = statuses.get(200, 0)
                print(f"[{label}] {done_count}/{len(addrs)} "
                      f"({rate:.0f}/с, ETA {eta / 60:.1f} мин) 200={ok200} статусы={statuses}",
                      flush=True)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="Массовый чек прокси-листа против Avito")
    ap.add_argument("file")
    ap.add_argument("--sample", type=int, default=0, help="проверить только N случайных")
    ap.add_argument("--workers", type=int, default=100)
    ap.add_argument("--timeout", type=float, default=6.0)
    ap.add_argument("--out", default="proxy_sweep_result.json")
    args = ap.parse_args()

    global TARGET
    addrs = parse_file(args.file)
    print(f"распарсено валидных уникальных: {len(addrs)}")
    if not addrs:
        return 1

    if args.sample and args.sample < len(addrs):
        addrs = random.sample(addrs, args.sample)
        print(f"sample: {len(addrs)} случайных")

    results = sweep(addrs, args.workers, "sweep")

    ok = [r for r in results if r.get("status") == 200]
    ok.sort(key=lambda r: r.get("ms", 999999))
    statuses = {}
    for r in results:
        st = r.get("status")
        statuses[str(st)] = statuses.get(str(st), 0) + 1
    print("\n=== ИТОГ ===")
    print(f"проверено: {len(results)} | 200: {len(ok)} | статусы: {statuses}")
    for r in ok[:30]:
        print(f"  OK {r['addr']} {r['ms']}ms bytes={r.get('bytes')}")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"checked": len(results), "statuses": statuses,
                   "working": ok, "all": results}, f, ensure_ascii=False)
    print(f"сохранено: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
