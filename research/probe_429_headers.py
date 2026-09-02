# language: Python, file: probe_429_headers.py
# Что говорит 429-ответ в заголовках? Retry-After? X-RateLimit? Qrator-специфика?
import sys

from curl_cffi import requests as curl_requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

s = curl_requests.Session(impersonate="safari15_5")
s.headers.update({"accept": "text/html,*/*;q=0.8", "accept-language": "ru-RU,ru;q=0.9"})
r = s.get("https://www.avito.ru/", timeout=25, allow_redirects=False)
print(f"status: {r.status_code}")
print("\n--- all headers ---")
for name, value in r.headers.items():
    print(f"{name}: {value[:100]}")
body = (r.content or b"")[:400].decode("utf-8", "replace")
print(f"\n--- body head ---\n{body}")
