import asyncio
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import avito_monitor_bot as appmod
import avito_monitoring as monitoring
import alert_bot
import avito_api
from storage import load_json, update_json


class FakeBot:
    async def send_message(self, *args, **kwargs):
        return None


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class FakeState:
    def __init__(self):
        self.data = {}
        self.state = None

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, state):
        self.state = state


class RegressionTests(unittest.TestCase):
    def test_regexes_and_newlines(self):
        key = "123e4567-e89b-12d3-a456-426614174000"
        self.assertEqual(appmod.KEY_RE.search(f"Ключ: {key}").group(1), key)
        self.assertEqual(appmod._br("a\\nb<br>c"), "a\nb\nc")
        self.assertEqual(appmod.avito_short_url("https://www.avito.ru/moskva/x_123456789"), "https://www.avito.ru/123456789")
        self.assertEqual(appmod._extract_ad_id("/moskva/telefony/iphone_15_123456789"), "123456789")

    def test_search_url_preserves_price_filters(self):
        url = (
            "https://www.avito.ru/all/telefony?context=large-value"
            "&q=samsung&pmin=10000&pmax=70000&params=one&params=two&utm_source=test"
        )

        normalized = appmod.search_key_from_url(url)
        visible = appmod.avito_short_url(normalized)

        self.assertIn("pmin=10000", normalized)
        self.assertIn("pmax=70000", normalized)
        self.assertEqual(normalized.count("params="), 2)
        self.assertNotIn("utm_source", normalized)
        self.assertIn("pmin=10000", visible)
        self.assertIn("pmax=70000", visible)
        self.assertIn("q=samsung", visible)
        self.assertNotIn("context=", visible)

        filters = appmod.try_extract_filters_from_url(normalized)
        self.assertEqual((filters.price_min, filters.price_max), (10000, 70000))

    def test_price_filter_is_extracted_from_avito_f_parameter(self):
        url = (
            "https://www.avito.ru/all/telefony/mobilnye_telefony/samsung-ASgBAgICAkS0wA2crzmwwQ2I_Dc"
            "?f=ASgBAgECAkS0wA2crzmwwQ2I_DcBRcaaDBV7ImZyb20iOjAsInRvIjo3MDAwMH0&q=samsung&s=104"
        )

        filters = appmod.try_extract_filters_from_url(url)

        self.assertIsNone(filters.price_min)
        self.assertEqual(filters.price_max, 70000)

    def test_wizard_uses_price_from_avito_url(self):
        async def scenario():
            message = FakeMessage(
                "https://www.avito.ru/all/telefony?q=samsung&pmin=10000&pmax=70000"
            )
            state = FakeState()

            await appmod.wizard_got_url(message, state)

            self.assertEqual(state.data["price_min"], 10000)
            self.assertEqual(state.data["price_max"], 70000)
            self.assertEqual(state.state, appmod.SearchWizard.name)
            self.assertIn("Цена от: 10 000 ₽", message.answers[-1][0])
            self.assertIn("Цена до: 70 000 ₽", message.answers[-1][0])

        asyncio.run(scenario())

    def test_subscription_panel_is_compact_and_menu_callbacks_exist(self):
        sub = appmod.Subscription(
            id=1,
            user_id=10,
            search_key="key",
            url="https://www.avito.ru/all/telefony/samsung_123456789?context=very-long-value",
            name="Samsung",
            flt=appmod.SubscriberFilter(
                price_min=10000,
                price_max=70000,
                keywords_all=["samsung"],
            ),
        )
        panel = appmod.format_sub_panel(sub, appmod.LicenseManager())
        self.assertNotIn("\\n", panel)
        self.assertNotIn("context=very-long-value", panel)
        self.assertIn("Открыть поиск на Avito", panel)

        callbacks = {
            button.callback_data
            for row in appmod.MAIN_INLINE_KB.inline_keyboard
            for button in row
        }
        self.assertEqual(callbacks, {"searches", "account", "help", "support"})

    def test_listing_parser_extracts_core_fields(self):
        payload = {"catalog": {"items": [{
            "id": 123456789,
            "urlPath": "/moskva/telefony/iphone_15_123456789",
            "title": "iPhone 15",
            "priceDetailed": {"value": 85000},
            "geo": {"formattedAddress": "Москва"},
            "description": "Новый телефон",
            "sortTimeStamp": 1735000000000,
            "gallery": {"imageLargeUrl": "https://example.test/a.jpg"},
        }]}}
        ads = appmod.parse_api_items(payload, 5)
        self.assertEqual(len(ads), 1)
        self.assertEqual((ads[0]["ad_id"], ads[0]["price"], ads[0]["location"]),
                         ("123456789", 85000, "\u041c\u043e\u0441\u043a\u0432\u0430"))
        self.assertIsNotNone(ads[0]["published_ts"])

    def test_avito_transport_has_persistent_cookie_jar(self):
        watcher = appmod.Watcher("key", "https://www.avito.ru/moskva", FakeBot())
        self.assertIsNotNone(watcher._client.session.cookies)
        watcher._client.close()

    def test_monitoring_classes_are_reexported_from_entrypoint(self):
        self.assertIs(appmod.Watcher, monitoring.Watcher)
        self.assertIs(appmod.WatcherManager, monitoring.WatcherManager)

    def test_warmup_raises_challenge_without_second_request(self):
        response = MagicMock(
            status_code=439,
            headers={"Content-Type": "text/html"},
            text="security challenge",
        )
        client = avito_api.AvitoHttpClient(timeout=1)
        client._session.get = MagicMock(return_value=response)
        with self.assertRaises(avito_api.AvitoBlock) as raised:
            client.get_items("https://www.avito.ru/web/1/js/items?q=test")
        self.assertEqual(raised.exception.kind, "challenge")
        self.assertEqual(client._session.get.call_count, 1)
        client.close()

    def test_block_classification_and_retry_after(self):
        block = avito_api.classify_block(403, {}, '{"too-many-requests": true}')
        self.assertEqual((block.kind, block.status), ("rate_limit", 403))
        block = avito_api.classify_block(302, {"Location": "/captcha"}, "")
        self.assertEqual(block.kind, "challenge")
        retry_at = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90))
        block = avito_api.classify_block(429, {"Retry-After": retry_at}, "")
        self.assertGreaterEqual(block.retry_after, 85)
        self.assertLessEqual(block.retry_after, 90)

    def test_api_url_validation(self):
        self.assertTrue(avito_api.is_valid_api_url("https://www.avito.ru/web/1/js/items?q=x"))
        self.assertFalse(avito_api.is_valid_api_url("https://evil.test/web/1/js/items"))
        self.assertFalse(avito_api.is_valid_api_url("http://www.avito.ru/web/1/js/items"))
        self.assertFalse(avito_api.is_valid_api_url("https://www.avito.ru/profile"))

    def test_invalid_cached_api_url_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "api.json")
            bad_url = "https://evil.test/web/1/js/items"
            update_json(path, {}, lambda data: data.__setitem__("https://www.avito.ru/moskva", bad_url))
            response = MagicMock(status_code=500)
            with patch("requests.post", return_value=response):
                self.assertIsNone(avito_api.convert_url_to_api("https://www.avito.ru/moskva", path))

    def test_fetch_ads_success_resets_block_state(self):
        async def scenario():
            watcher = appmod.Watcher("key", "https://www.avito.ru/moskva", FakeBot())
            watcher._api_url = "https://www.avito.ru/web/1/js/items?q=test"
            watcher._consecutive_blocks = 2
            watcher.last_block_kind = "ip_block"
            watcher._client.last_status = 200
            watcher._client.get_items = MagicMock(return_value={"catalog": {"items": [{
                "id": 1, "urlPath": "/moskva/x_1234567", "title": "Phone"
            }]}})
            with patch.object(watcher, "_wait_global_rate_limit", AsyncMock()):
                ads = await watcher._fetch_ads()
            self.assertEqual([ad.ad_id for ad in ads], ["1"])
            self.assertEqual(watcher._consecutive_blocks, 0)
            self.assertIsNone(watcher.last_block_kind)
            watcher._client.close()
        asyncio.run(scenario())

    def test_fetch_ads_block_applies_cooldown_and_resets_session(self):
        async def scenario():
            appmod.Watcher._route_blocked_until.clear()
            watcher = appmod.Watcher("key", "https://www.avito.ru/moskva", FakeBot())
            watcher._api_url = "https://www.avito.ru/web/1/js/items?q=test"
            watcher._client.get_items = MagicMock(side_effect=avito_api.AvitoBlock("ip_block", 429, 30))
            watcher._client.reset = MagicMock()
            watcher._client.request_new_ip = MagicMock(return_value=False)
            with patch.object(watcher, "_wait_global_rate_limit", AsyncMock()), patch("avito_api.random.uniform", return_value=0):
                before = time.monotonic()
                ads = await watcher._fetch_ads()
            self.assertIsNone(ads)
            self.assertEqual(watcher.last_block_kind, "ip_block")
            self.assertEqual(watcher.last_http_status, 429)
            self.assertGreaterEqual(watcher._blocked_until, before + 30)
            watcher._client.reset.assert_called_once()
            watcher._client.close()
        asyncio.run(scenario())

    def test_block_cooldown_is_shared_by_watchers_on_same_route(self):
        async def scenario():
            appmod.Watcher._route_blocked_until.clear()
            first = appmod.Watcher("one", "https://www.avito.ru/moskva", FakeBot())
            second = appmod.Watcher("two", "https://www.avito.ru/spb", FakeBot())
            first._api_url = "https://www.avito.ru/web/1/js/items?q=one"
            second._api_url = "https://www.avito.ru/web/1/js/items?q=two"
            first._client.get_items = MagicMock(side_effect=avito_api.AvitoBlock("challenge", 439, 30))
            second._client.get_items = MagicMock(return_value={"catalog": {"items": []}})
            first._client.request_new_ip = MagicMock(return_value=False)
            with patch.object(first, "_wait_global_rate_limit", AsyncMock()), patch("avito_api.random.uniform", return_value=0):
                await first._fetch_ads()
            with patch.object(second, "_wait_global_rate_limit", AsyncMock()):
                self.assertIsNone(await second._fetch_ads())
            second._client.get_items.assert_not_called()
            first._client.close()
            second._client.close()
            appmod.Watcher._route_blocked_until.clear()
        asyncio.run(scenario())

    def test_fetch_ads_rotates_proxy_and_retries(self):
        async def scenario():
            appmod.Watcher._route_blocked_until.clear()
            with patch.object(monitoring, "AVITO_PROXIES", ["http://one", "http://two"]), \
                    patch.object(monitoring, "AVITO_PROXY_CHANGE_URLS", []):
                watcher = appmod.Watcher("key", "https://www.avito.ru/moskva", FakeBot())
                watcher._proxy_index = 0
                watcher._api_url = "https://www.avito.ru/web/1/js/items?q=test"
                watcher._client.get_items = MagicMock(side_effect=[
                    avito_api.AvitoBlock("challenge", 439, 1),
                    {"catalog": {"items": [{"id": 2, "urlPath": "/x_2345678", "title": "OK"}]}},
                ])
                watcher._client.request_new_ip = MagicMock(return_value=False)
                watcher._client.set_proxy = MagicMock()
                watcher._client.last_status = 200
                with patch.object(watcher, "_wait_global_rate_limit", AsyncMock()):
                    ads = await watcher._fetch_ads()
                self.assertEqual([ad.ad_id for ad in ads], ["2"])
                self.assertEqual(watcher._proxy_index, 1)
                watcher._client.set_proxy.assert_called_once_with("http://two", None)
                watcher._client.close()
        asyncio.run(scenario())

    def test_stale_api_url_is_invalidated(self):
        async def scenario():
            appmod.Watcher._route_blocked_until.clear()
            watcher = appmod.Watcher("key", "https://www.avito.ru/moskva", FakeBot())
            watcher._api_url = "https://www.avito.ru/web/1/js/items?q=stale"
            watcher._client.get_items = MagicMock(side_effect=avito_api.AvitoHttpError(404, "gone"))
            with patch.object(watcher, "_wait_global_rate_limit", AsyncMock()), \
                    patch.object(monitoring, "invalidate_cached_api_url") as invalidate:
                self.assertIsNone(await watcher._fetch_ads())
            self.assertIsNone(watcher._api_url)
            invalidate.assert_called_once_with(watcher.url, appmod.API_URLS_FILE)
            watcher._client.close()
            appmod.Watcher._route_blocked_until.clear()
        asyncio.run(scenario())

    def test_avito_url_validation_blocks_ssrf_hosts(self):
        self.assertTrue(appmod.is_valid_avito_url("https://www.avito.ru/moskva"))
        self.assertFalse(appmod.is_valid_avito_url("https://evilavito.ru/"))
        self.assertFalse(appmod.is_valid_avito_url("http://127.0.0.1/"))
        self.assertFalse(appmod.is_valid_avito_url("https://avito.ru.evil.test/"))

    def test_two_subscriptions_for_same_user_are_retained(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                manager = appmod.WatcherManager(FakeBot(), str(Path(tmp) / "subs.json"))
                with patch.object(appmod.Watcher, "start", AsyncMock()), patch.object(appmod.Watcher, "stop", AsyncMock()):
                    first = await manager.add_subscription(7, "https://www.avito.ru/moskva?q=one")
                    second = await manager.add_subscription(7, "https://www.avito.ru/moskva?q=one")
                    watcher = manager.watchers[first.search_key]
                    self.assertEqual(set(watcher.subscribers), {first.id, second.id})
                    await manager.remove_subscription(7, first.id)
                    self.assertIn(second.id, watcher.subscribers)
                    self.assertIn(first.search_key, manager.watchers)
        asyncio.run(scenario())

    def test_restore_subscriptions(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                path = str(Path(tmp) / "subs.json")
                manager = appmod.WatcherManager(FakeBot(), path)
                with patch.object(appmod.Watcher, "start", AsyncMock()):
                    original = await manager.add_subscription(9, "https://www.avito.ru/moskva?q=test")
                    restored = appmod.WatcherManager(FakeBot(), path)
                    await restored.restore()
                    self.assertEqual(restored.list_user_subs(9)[0].id, original.id)
        asyncio.run(scenario())

    def test_json_update_preserves_sequential_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.json")
            update_json(path, {}, lambda data: data.__setitem__("first", 1))
            update_json(path, {}, lambda data: data.__setitem__("second", 2))
            self.assertEqual(load_json(path, {}), {"first": 1, "second": 2})

    def test_json_storage_recovers_from_stale_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.json")
            lock_path = path + ".lock"
            Path(lock_path).touch()
            stale_time = time.time() - 60
            os.utime(lock_path, (stale_time, stale_time))

            update_json(path, {}, lambda data: data.__setitem__("restored", True))

            self.assertEqual(load_json(path, {}), {"restored": True})
            self.assertFalse(Path(lock_path).exists())

    def test_alert_binding_token_is_one_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "links.json")
            with patch.object(alert_bot, "ALERT_LINKS_FILE", path):
                update_json(path, {}, lambda data: data.__setitem__(
                    "secret", {"user_id": 777, "expires": time.time() + 60}))
                self.assertEqual(alert_bot._consume_link_token("secret"), 777)
                self.assertIsNone(alert_bot._consume_link_token("secret"))
                self.assertIsNone(alert_bot._consume_link_token("777"))


if __name__ == "__main__":
    unittest.main()
