# language: Python, file: probe_integration.py
# Живой smoke: AvitoHttpClient.get_items с встренным PoW-решателем.
import json

from avito_api import AvitoHttpClient

API_URL = (
    "https://www.avito.ru/web/1/js/items?categoryId=84&localPriority=0"
    "&locationId=621540&presentationType=serp&query=samsung&sort=date"
)

if __name__ == "__main__":
    client = AvitoHttpClient()
    data = client.get_items(API_URL)
    items = data.get("catalog", {}).get("items", [])
    out = {
        "items": len(items),
        "ok": client.total_ok,
        "blocked": client.total_blocked,
        "pow_solved": getattr(client, "total_pow_solved", 0),
        "last_status": client.last_status,
    }
    if items:
        first = items[0]
        out["first_title"] = (first.get("title") or "")[:60]
    print(json.dumps(out, ensure_ascii=False))
    client.close()
