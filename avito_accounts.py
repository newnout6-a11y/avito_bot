"""Account, license-key, and license-expiry persistence."""

import logging
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from avito_settings import ACCOUNTS_FILE, DISPLAY_TZ_NAME, DISPLAY_TZ_OFFSET_MIN, KEYS_FILE
from storage import load_state, save_state, update_state

logger = logging.getLogger(__name__)


class LicenseManager:
    """In-memory active-license projection used by the bot process."""

    def __init__(self) -> None:
        self._expires: Dict[int, float] = {}

    def activate_for(self, user_id: int, hours: int = 24) -> None:
        self._expires[user_id] = time.time() + hours * 3600

    def activate_until(self, user_id: int, expires_ts: float) -> None:
        self._expires[user_id] = float(expires_ts)

    def is_active(self, user_id: int) -> bool:
        expires_ts = self._expires.get(user_id)
        return expires_ts is not None and expires_ts > time.time()

    def expiry_dt(self, user_id: int) -> Optional[datetime]:
        expires_ts = self._expires.get(user_id)
        if not expires_ts:
            return None
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(DISPLAY_TZ_NAME)
        except Exception:
            from datetime import timedelta, timezone

            tz = timezone(timedelta(minutes=DISPLAY_TZ_OFFSET_MIN))
        return datetime.fromtimestamp(expires_ts, tz)


class AccountService:
    """Owns account records and the in-memory license projection."""

    def __init__(
        self,
        license_manager: Optional[LicenseManager] = None,
        *,
        accounts_file: str = ACCOUNTS_FILE,
        keys_file: str = KEYS_FILE,
    ) -> None:
        self.license = license_manager or LicenseManager()
        self.accounts_file = accounts_file
        self.keys_file = keys_file

    def _load_accounts(self) -> Dict[str, Dict[str, Any]]:
        try:
            value = load_state(self.accounts_file, {}) or {}
            return {str(key): dict(record) for key, record in value.items()}
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Не удалось прочитать аккаунты: %s", exc)
            return {}

    def _restore_licenses(self) -> None:
        now = time.time()
        for user_id, account in self._load_accounts().items():
            expires = max(
                (float(record.get("expires", 0)) for record in account.get("keys", [])),
                default=0,
            )
            if expires > now:
                self.license.activate_until(int(user_id), expires)

    def _load_keys(self) -> Dict[str, Dict[str, Any]]:
        try:
            value = load_state(self.keys_file, {}) or {}
            return {str(key): dict(record) for key, record in value.items()}
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Не удалось прочитать ключи: %s", exc)
            return {}

    def _save_keys(self, data: Dict[str, Dict[str, Any]]) -> None:
        save_state(self.keys_file, data)

    def register_if_needed(self, user_id: int) -> None:
        def register(data: Dict[str, Any]) -> None:
            data.setdefault(str(user_id), {"registered": time.time(), "keys": []})

        update_state(self.accounts_file, {}, register)

    def get(self, user_id: int) -> Optional[Dict[str, Any]]:
        return self._load_accounts().get(str(user_id))

    def issue_key(self, hours: int = 24, uses: int = 1) -> str:
        key = str(uuid.uuid4())

        def issue(data: Dict[str, Any]) -> None:
            data[key] = {
                "hours": int(hours),
                "uses_left": int(uses),
                "created": time.time(),
            }

        update_state(self.keys_file, {}, issue)
        return key

    def redeem_key(self, key: str) -> Optional[tuple[int, float]]:
        def redeem(data: Dict[str, Any]) -> Optional[tuple[int, float]]:
            record = data.get(key)
            if not record or int(record.get("uses_left", 0)) <= 0:
                return None
            hours = int(record.get("hours", 24))
            expires = time.time() + hours * 3600
            record["uses_left"] = int(record["uses_left"]) - 1
            return hours, float(expires)

        return update_state(self.keys_file, {}, redeem)

    def add_key(
        self, user_id: int, key_value: str, hours: int, expires_ts: Optional[float]
    ) -> None:
        def add(data: Dict[str, Any]) -> None:
            account = data.setdefault(str(user_id), {"registered": time.time(), "keys": []})
            account.setdefault("keys", []).append(
                {
                    "key": key_value,
                    "activated": time.time(),
                    "hours": int(hours),
                    "expires": float(expires_ts or 0.0),
                }
            )

        update_state(self.accounts_file, {}, add)
