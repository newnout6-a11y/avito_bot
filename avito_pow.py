# language: Python, file: avito_pow.py
# Решатель Avito firewall PoW (429/439 challenge) без браузера.
# Механика (из research/challenge_page.html, живой захват 2026-08-27):
#   1) 439-ответ ставит куку pow_challenge
#   2) POST /web/3/firewallPow/get {challenge} -> challenge_jwt
#   3) JWT payload: {id, compl} — ищем nonce: sha256(f"{id}:{nonce}") с compl ведущими hex-нулями
#   4) POST /web/3/firewallPow/verify {challenge: jwt, nonce} -> verified
#   5) перезапрос исходного URL проходит
import base64
import hashlib
import json
import logging
import time

logger = logging.getLogger(__name__)

GET_URL = "https://www.avito.ru/web/3/firewallPow/get"
VERIFY_URL = "https://www.avito.ru/web/3/firewallPow/verify"


def _b64url_json(segment: str) -> dict:
    pad = "=" * (-len(segment) % 4)
    raw = base64.urlsafe_b64decode(segment + pad)
    return json.loads(raw.decode("utf-8"))


def solve_nonce(challenge_id: str, complexity: int) -> int:
    """Перебор nonce: sha256(id:nonce) с complexity ведущими hex-нулями."""
    prefix = "0" * complexity
    nonce = 0
    t0 = time.time()
    while True:
        digest = hashlib.sha256(f"{challenge_id}:{nonce}".encode()).hexdigest()
        if digest.startswith(prefix):
            dt = time.time() - t0
            logger.info("pow solved: nonce=%d, complexity=%d, %.2fs", nonce, complexity, dt)
            return nonce
        nonce += 1


def solve_pow_challenge(session, block_response) -> bool:
    """
    Пройти PoW в переданной curl_cffi-сессии (куки уже должны содержать
    pow_challenge из block_response). True — проверка пройдена.
    """
    # кука могла приехать в самом block_response — curl_cffi кладёт её в session.cookies
    cookies = dict(session.cookies)
    challenge = cookies.get("pow_challenge")
    if not challenge:
        # иногда кука приезжает только с ответом — вытащим заголовки
        set_cookie = block_response.headers.get("set-cookie", "") if block_response else ""
        for part in set_cookie.split(","):
            if "pow_challenge=" in part:
                challenge = part.split("pow_challenge=", 1)[1].split(";", 1)[0].strip()
                session.cookies.set("pow_challenge", challenge)
                break
    if not challenge:
        logger.warning("pow_challenge cookie не найдена")
        return False

    r = session.post(
        GET_URL,
        json={"challenge": challenge},
        timeout=15,
        headers={"content-type": "application/json"},
    )
    data = r.json()
    jwt = ((data or {}).get("success") or {}).get("result", {}).get("challenge_jwt")
    if not jwt:
        logger.warning("firewallPow/get без challenge_jwt: %s", str(data)[:200])
        return False

    payload = _b64url_json(jwt.split(".")[1])
    cid = payload["id"]
    compl = int(payload["compl"])

    nonce = solve_nonce(cid, compl)

    r = session.post(
        VERIFY_URL,
        json={"challenge": jwt, "nonce": nonce},
        timeout=15,
        headers={"content-type": "application/json"},
    )
    vdata = r.json()
    verified = ((vdata or {}).get("success") or {}).get("result", {}).get("verified")
    if verified:
        session.cookies.set("pow_solved", "1", domain=".avito.ru", path="/")
    logger.info("firewallPow/verify: verified=%s", verified)
    return bool(verified)
