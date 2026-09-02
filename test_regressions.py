import asyncio
import base64
import hashlib
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import alert_bot
import avito_api
import avito_domain
import avito_monitor_bot as appmod
import avito_monitoring as monitoring
import avito_pow
import avito_sitemap
import avito_ui
import proxy_harvest
from avito_accounts import AccountService, LicenseManager
from storage import load_json, load_state, update_json, update_state


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

    async def get_data(self):
        return dict(self.data)

    async def set_state(self, state):
        self.state = state


class FakeDeliveryApp:
    def __init__(self):
        self.license = MagicMock()
        self.license.is_active.return_value = True
        self.user_sent = set()
        self.deliveries = []

    def sent_was_delivered(self, user_id, ad_id):
        return (user_id, ad_id) in self.user_sent

    def sent_mark(self, user_id, ad_id, ts=None):
        self.user_sent.add((user_id, ad_id))

    def get_alert_chat_id(self, user_id):
        return user_id

    async def send_to_alert(self, chat_id, caption, image_url):
        self.deliveries.append(chat_id)
        return True

    def missing_alert_hint_once(self, user_id):
        return False


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

    def test_user_mobile_search_url_is_supported(self):
        url = (
            "https://www.avito.ru/all/telefony/mobile-ASgBAgICAUSwwQ2I_Dc"
            "?context=H4sIAAAAAAAA_wEmANn_YToxOntzOjE6InkiO3M6MTY6IndxRTdQdVlyNUhUWkVENmIiO30jnPp_JgAAAA"
            "&f=ASgBAgECAUSwwQ2I_DcBRcaaDBV7ImZyb20iOjAsInRvIjo3MDAwMH0"
        )
        spec = appmod.parse_avito_url(url)
        filters = appmod.try_extract_filters_from_url(url)
        self.assertEqual(spec.kind, "search")
        self.assertEqual(filters.price_max, 70000)
        self.assertEqual(spec.sort_title, "По умолчанию")

    def test_avito_sort_is_reported_separately_from_only_new(self):
        self.assertEqual(
            appmod.parse_avito_url("https://www.avito.ru/moskva?s=104").sort_title,
            "По дате",
        )
        self.assertEqual(
            appmod.parse_avito_url("https://www.avito.ru/moskva?s=1").sort_title,
            "Дешевле",
        )
        self.assertEqual(
            appmod.parse_avito_url("https://www.avito.ru/moskva?s=2").sort_title,
            "Дороже",
        )

    def test_price_filter_is_extracted_when_avito_splits_f_with_tilde(self):
        payload = {"from": 0, "to": 70000}
        encoded = base64.urlsafe_b64encode(
            b"\x01(\x01\x01\x01\x02\x02D\xb4\xc0\r\x9c\xaf9\xb0\xc3\x04"
            b"6#\xf0\xdc\x05\x03\xa3\xac8\x93\xeb\xf7l\x08\xfd\xdb\x02\x01E\xc6\x9a\x0c\x15"
            + json.dumps(payload, separators=(",", ":")).encode()
        ).decode().rstrip("=")
        split = f"{encoded[:41]}~{encoded[41:]}"

        filters = appmod.try_extract_filters_from_url(
            f"https://www.avito.ru/all/telefony?f={split}&q=samsung"
        )

        self.assertIsNone(filters.price_min)
        self.assertEqual(filters.price_max, 70000)
        self.assertEqual(filters.keywords_all, ["samsung"])

    def test_screenshot_avito_link_has_no_filter_warning(self):
        url = (
            "https://www.avito.ru/all/telefony/mobilnye_telefony/samsung-"
            "ASgBAgICAgS0wA2crzmwwwQ2I_Dc?cd=1&context=H4sIAAAAAAAA_wEmANn_YToxOntzOjE6InkiO3M6MTY6IR5d0dNTkRnTUZQejVBOFciO32swxHelgAAAA"
            "&f=ASgBAQECAkS0wA2crzmwwwQ2I_DcBQOjrDiT6_dsC~P3bAgFFxpoMFXsiZnJvbSI6MCwidG8iOjcwMDAwfQ"
            "&q=samsung&s=104"
        )

        filters, warnings = avito_domain.parse_filters(url)

        self.assertEqual((filters.price_min, filters.price_max), (None, 70000))
        self.assertEqual(filters.keywords_all, ["samsung"])
        self.assertNotIn("Не удалось разобрать фильтр f", " ".join(warnings))

    def test_real_avito_search_link_with_multisegment_f(self):
        url = (
            "https://www.avito.ru/all/telefony/mobilnye_telefony/samsung-"
            "ASgBAgICAkS0wA2crzmwwQ2I_Dc?cd=1&context=H4sIAAAAAAAA_wEmANn_YToxOntzOjE6InkiO3M6MTY6IjRnQUlxVlU3SFhyNVB2RmwiO3094LcAJgAAAA"
            "&f=ASgBAQECAkS0wA2crzmwwQ2I_DcBQOjrDiT6_dsC~P3bAgFFxpoMFXsiZnJvbSI6MCwidG8iOjcwMDAwfQ"
            "&q=samsung&s=104"
        )
        spec = avito_domain.parse_avito_url(url)
        self.assertEqual(spec.filters.price_min, None)
        self.assertEqual(spec.filters.price_max, 70000)
        self.assertEqual(spec.filters.keywords_all, ["samsung"])
        self.assertEqual(spec.warnings, ())

    def test_real_avito_search_link_with_color_filter(self):
        url = (
            "https://www.avito.ru/all/telefony/mobilnye_telefony/samsung/belyy-"
            "ASgBAgICA0SwwA3u_ze0wA2crzmwwQ2I_Dc?cd=1&context=H4sIAAAAAAAA_wEmANn_YToxOntzOjE6InkiO3M6MTY6Imp0T2xTNk5sTWtwM3plVlUiO31lmxiJJgAAAA"
            "&f=ASgBAQECA0SwwA3u_ze0wA2crzmwwQ2I_DcDQOTgDTSMwlyOwlyQwlzo6w4k~v3bAvj92wK0xRYkpL2XA6a9lwMBRcaaDBV7ImZyb20iOjAsInRvIjo3MDAwMH0"
            "&q=samsung&s=104"
        )
        spec = avito_domain.parse_avito_url(url)
        self.assertEqual(spec.filters.price_min, None)
        self.assertEqual(spec.filters.price_max, 70000)
        self.assertEqual(spec.filters.keywords_all, ["samsung"])
        self.assertEqual(spec.warnings, ())

    def test_wizard_uses_price_from_avito_url(self):
        async def scenario():
            message = FakeMessage(
                "https://www.avito.ru/all/telefony?q=samsung&pmin=10000&pmax=70000"
            )
            state = FakeState()

            await appmod.wizard_got_url(message, state)

            self.assertEqual(state.data["price_min"], 10000)
            self.assertEqual(state.data["price_max"], 70000)
            self.assertEqual(state.state, appmod.SearchWizard.confirm)
            self.assertIn("Цена: 10 000 ₽ — 70 000 ₽", message.answers[-1][0])
            self.assertIn("Нормализованная ссылка", message.answers[-1][0])

        asyncio.run(scenario())

    def test_confirm_preserves_min_only_price_from_url(self):
        async def scenario():
            message = FakeMessage(
                "https://www.avito.ru/all/telefony?q=samsung&pmin=10000"
            )
            state = FakeState()

            await appmod.wizard_got_url(message, state)
            self.assertEqual(state.data["price_min"], 10000)
            self.assertIsNone(state.data["price_max"])

            await avito_ui._continue_after_filter_review(message, state)

            # A detected minimum price must not be discarded by re-prompting
            # for it; the wizard should proceed straight to the name step.
            self.assertEqual(state.state, appmod.SearchWizard.name)
            self.assertEqual(state.data["price_min"], 10000)
            self.assertIsNone(state.data["price_max"])
            self.assertIn("Цена от: 10 000 ₽", message.answers[-1][0])

        asyncio.run(scenario())

    def test_words_edit_clears_keywords_despite_guessed_hint(self):
        async def scenario():
            message = FakeMessage(
                "https://www.avito.ru/all/telefony?q=samsung"
            )
            state = FakeState()

            await appmod.wizard_got_url(message, state)
            self.assertEqual(state.data["keywords_all"], ["samsung"])
            self.assertEqual(state.data["guessed_kw"], ["samsung"])

            # The user explicitly clears the required-keywords list.
            clear_message = FakeMessage("-")
            await avito_ui.wizard_got_words(clear_message, state)

            self.assertEqual(state.data["keywords_all"], [])
            # The originally guessed keyword must not resurface once the
            # user explicitly cleared the list.
            filters = avito_ui._filter_from_data(state.data)
            self.assertEqual(filters.keywords_all, [])

        asyncio.run(scenario())

    def test_url_parser_table(self):
        cases = [
            ("avito.ru/moskva?q=x", "https://avito.ru/moskva?q=x"),
            ("http://www.avito.ru/moskva?q=x", "https://avito.ru/moskva?q=x"),
            ("https://m.avito.ru/moskva?q=x", "https://m.avito.ru/moskva?q=x"),
            ("https://avito.ru/moskva?q=x).", "https://avito.ru/moskva?q=x"),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(appmod.parse_avito_url(source).canonical_url, expected)

        first = appmod.parse_avito_url(
            "https://avito.ru/moskva?b=2&a=3&a=1&utm_source=test"
        )
        second = appmod.parse_avito_url(
            "https://avito.ru/moskva?a=1&a=3&b=2"
        )
        self.assertEqual(first.search_key, second.search_key)
        self.assertEqual(first.canonical_url.count("a="), 2)
        self.assertNotIn("utm_source", first.canonical_url)
        self.assertEqual(
            avito_ui._extract_avito_url_text(
                "Вот ссылка https://m.avito.ru/moskva?q=x), проверь"
            ),
            "https://m.avito.ru/moskva?q=x),",
        )
        self.assertIsNone(
            avito_ui._extract_avito_url_text("ftp://avito.ru/moskva")
        )

    def test_url_parser_rejects_unsafe_and_item_urls(self):
        invalid = [
            "https://user:pass@avito.ru/moskva",
            "https://avito.ru:8443/moskva",
            "https://avito.ru/moskva#fragment",
            "https://avito.ru.evil.test/moskva",
            "ftp://avito.ru/moskva",
        ]
        for source in invalid:
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    appmod.parse_avito_url(source)
        item = appmod.parse_avito_url(
            "https://www.avito.ru/moskva/telefony/iphone_123456789"
        )
        self.assertEqual(item.kind, "item")

    def test_filter_parser_reports_malformed_f_and_normalizes_words(self):
        malformed = appmod.parse_avito_url("https://avito.ru/moskva?f=not_base64%25")
        self.assertTrue(any("filter f" in warning or "фильтр f" in warning for warning in malformed.warnings))

        payload = {"from": 1000, "to": 5000, "brand": "Samsung samsung"}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        parsed = appmod.parse_avito_url(
            f"https://avito.ru/moskva?q=SAMSUNG&f={encoded}"
        )
        self.assertEqual(parsed.filters.keywords_all, ["SAMSUNG"])
        self.assertEqual((parsed.filters.price_min, parsed.filters.price_max), (1000, 5000))

    def test_local_filter_modes(self):
        ad = appmod.Ad(
            ad_id="1",
            url="https://avito.ru/1",
            title="Samsung Galaxy",
            description="256GB",
            price=50000,
        )
        passing = appmod.Subscription(
            1,
            1,
            "key",
            "https://avito.ru/moskva",
            appmod.SubscriberFilter(
                keywords_all=["Samsung", "256GB"],
                keywords_any=["iPhone", "Galaxy"],
                price_min=40000,
                price_max=60000,
            ),
        )
        blocked = appmod.Subscription(
            2,
            1,
            "key",
            "https://avito.ru/moskva",
            appmod.SubscriberFilter(keywords_stop=["Galaxy"]),
        )
        self.assertTrue(appmod.Watcher._ad_passes_filters(ad, passing))
        self.assertFalse(appmod.Watcher._ad_passes_filters(ad, blocked))

    def test_keyword_filter_ignores_latin_cyrillic_script(self):
        ad = appmod.Ad(
            ad_id="1",
            url="https://avito.ru/1",
            title="Смартфон Самсунг Галакси S23",
            description="новый",
        )
        # latin query keyword matches a cyrillic listing
        latin = appmod.Subscription(
            1, 1, "key", "https://avito.ru/moskva",
            appmod.SubscriberFilter(keywords_all=["Samsung"]),
        )
        self.assertTrue(appmod.Watcher._ad_passes_filters(ad, latin))
        # and a cyrillic stop-word blocks a latin listing
        latin_ad = appmod.Ad(
            ad_id="2", url="https://avito.ru/2", title="Samsung Galaxy", description="",
        )
        stop = appmod.Subscription(
            2, 1, "key", "https://avito.ru/moskva",
            appmod.SubscriberFilter(keywords_stop=["самсунг"]),
        )
        self.assertFalse(appmod.Watcher._ad_passes_filters(latin_ad, stop))

    def test_add_options_preserve_full_filter_contract(self):
        spec = appmod.parse_avito_url(
            "https://avito.ru/moskva?q=Samsung&pmin=10000&pmax=70000"
        )
        filters, warnings = avito_ui._parse_add_options(
            "min=20000,max=-,any=256GB|512GB,stop=копия|ремонт",
            spec.filters,
        )
        self.assertEqual(filters.keywords_all, ["Samsung"])
        self.assertEqual(filters.keywords_any, ["256GB", "512GB"])
        self.assertEqual(filters.keywords_stop, ["копия", "ремонт"])
        self.assertEqual(filters.price_min, 20000)
        self.assertIsNone(filters.price_max)
        self.assertEqual(warnings, [])
        _, warnings = avito_ui._parse_add_options(
            "min=90000,max=10000", spec.filters
        )
        self.assertIn("Минимальная цена не может быть больше максимальной", warnings)

    def test_subscription_panel_is_compact_and_menu_callbacks_exist(self):
        sub = appmod.Subscription(
            id=1,
            user_id=10,
            search_key="key",
            url=(
                "https://www.avito.ru/all/telefony/samsung_123456789"
                "?context=very-long-value&s=104"
            ),
            name="Samsung",
            flt=appmod.SubscriberFilter(
                price_min=10000,
                price_max=70000,
                keywords_all=["samsung"],
                keywords_any=["256GB"],
            ),
        )
        panel = appmod.format_sub_panel(sub, appmod.LicenseManager())
        self.assertNotIn("\\n", panel)
        self.assertNotIn("context=very-long-value", panel)
        self.assertIn("Открыть поиск на Avito", panel)
        self.assertIn("Хотя бы одно: 256GB", panel)
        self.assertIn("Сортировка Avito: <b>По дате</b>", panel)
        self.assertIn("Только новые после запуска: <b>включено</b>", panel)

        sub_callbacks = {
            button.callback_data
            for row in appmod.build_sub_inline_kb(sub).inline_keyboard
            for button in row
        }
        self.assertTrue(
            {"sub:1:min", "sub:1:max", "sub:1:pos", "sub:1:any", "sub:1:stop"}
            <= sub_callbacks
        )

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
        self.assertTrue(ads[0]["published_exact"])

    def test_html_parser_uses_embedded_catalog_timestamp_and_promotion(self):
        state = {
            "loaderData": {
                "data": {
                    "catalog": {
                        "items": [
                            {
                                "id": 123456789,
                                "urlPath": "/moskva/telefony/iphone_123456789",
                                "title": "Promoted new phone",
                                "sortTimeStamp": 1735000000000,
                                "iva": {
                                    "DateInfoStep": [
                                        {
                                            "componentData": {"component": "date-info"},
                                            "payload": {
                                                "absolute": "",
                                                "relative": "1 час назад",
                                            },
                                        },
                                        {
                                            "componentData": {"component": "vas"},
                                            "payload": {
                                                "vas": [
                                                    {
                                                        "slug": "CPX_PROMO_V1",
                                                        "title": "Продвинуто",
                                                    }
                                                ]
                                            },
                                        },
                                    ],
                                    "DescriptionStep": [
                                        {
                                            "componentData": {"component": "description"},
                                            "payload": {"description": "From embedded state"},
                                        }
                                    ],
                                },
                            },
                            {
                                "id": 234567890,
                                "urlPath": "/moskva/telefony/unknown_234567890",
                                "title": "Unknown time",
                            },
                        ]
                    }
                }
            }
        }
        html_text = (
            '<html><script type="mime/invalid">'
            + json.dumps(state, ensure_ascii=False)
            + "</script></html>"
        )
        ads = avito_api.parse_html_feed(html_text)
        self.assertEqual([item["ad_id"] for item in ads], ["123456789", "234567890"])
        self.assertEqual(ads[0]["date_str"], "1 час назад")
        self.assertEqual(ads[0]["description"], "From embedded state")
        self.assertTrue(ads[0]["published_exact"])
        self.assertTrue(ads[0]["is_promoted"])
        self.assertIsNone(ads[1]["published_ts"])
        self.assertFalse(ads[1]["published_exact"])

    def test_feed_parser_variants_limit_and_health(self):
        payload = {
            "result": {
                "items": [
                    {"type": "banner"},
                    {
                        "id": 1,
                        "uriPath": "/moskva/one_1234567",
                        "title": "One",
                        "price": "10 000",
                        "location": {"title": "Москва"},
                        "sortTimeStamp": "1735000000",
                    },
                    {
                        "id": 2,
                        "urlPath": "/moskva/two_2345678",
                        "title": "Two",
                        "priceValue": 20000,
                        "location": "Москва",
                        "sortTimeStamp": 1735000000000,
                    },
                ]
            }
        }
        parsed = avito_api.parse_api_feed(payload, limit=1)
        self.assertEqual(len(parsed.items), 1)
        self.assertEqual(parsed.items[0]["price"], 10000)
        self.assertEqual(parsed.items[0]["location"], "Москва")
        self.assertEqual(parsed.skipped_items, 1)
        self.assertFalse(parsed.schema_mismatch)

        mismatch = avito_api.parse_api_feed({"unexpected": []})
        self.assertTrue(mismatch.schema_mismatch)
        self.assertTrue(mismatch.warnings)
        external = avito_api.parse_api_feed(
            {
                "items": [
                    {
                        "id": 3,
                        "urlPath": "https://evil.test/item_3456789",
                        "title": "External",
                    }
                ]
            }
        )
        self.assertEqual(external.items, [])
        self.assertEqual(external.skipped_items, 1)

    def test_route_resolver_migrates_legacy_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "api.json")
            public = "https://avito.ru/moskva?q=x"
            route = "https://www.avito.ru/web/1/js/items?q=x"
            update_json(path, {}, lambda data: data.__setitem__(public, route))

            resolver = avito_api.DefaultApiRouteResolver(path)
            self.assertEqual(resolver.resolve(public), route + "&sort=date")
            metadata = load_json(path, {})[public]
            self.assertEqual(metadata["api_url"], route)
            self.assertEqual(resolver.last_status, "ready")
            resolver.invalidate(public, "schema mismatch")
            invalidated = load_json(path, {})[public]
            self.assertIsNone(invalidated["api_url"])
            self.assertEqual(invalidated["last_error"], "schema mismatch")
            self.assertEqual(invalidated["fail_count"], 1)

    def test_route_resolver_uses_direct_api_url_without_spfa(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolver = avito_api.DefaultApiRouteResolver(str(Path(tmp) / "api.json"))
            direct = "https://www.avito.ru/web/1/js/items?q=x"
            with patch.object(avito_api, "_request_api_route") as request_route:
                self.assertEqual(resolver.resolve(direct), direct + "&sort=date")
            request_route.assert_not_called()
            self.assertEqual(resolver.last_status, "ready")

    def test_route_resolver_replaces_default_sort(self):
        route = "https://www.avito.ru/web/1/js/items?q=x&sort=default&sort=old"
        normalized = avito_api._ensure_sort_date(route)
        self.assertIn("sort=date", normalized)
        self.assertNotIn("sort=default", normalized)
        self.assertNotIn("sort=old", normalized)

    def test_route_resolver_keeps_stale_route_on_transient_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "api.json")
            public = "https://avito.ru/moskva?q=x"
            route = "https://www.avito.ru/web/1/js/items?q=x"
            update_json(
                path,
                {},
                lambda data: data.__setitem__(
                    public,
                    {
                        "api_url": route,
                        "created_at": 1,
                        "last_success_at": "bad",
                        "fail_count": "bad",
                    },
                ),
            )
            resolver = avito_api.DefaultApiRouteResolver(path)
            with patch.object(avito_api, "_request_api_route", return_value=(None, "offline")):
                self.assertEqual(resolver.resolve(public), route + "&sort=date")
            metadata = load_json(path, {})[public]
            self.assertEqual(metadata["api_url"], route + "&sort=date")
            self.assertEqual(metadata["last_error"], "offline")
            self.assertEqual(metadata["fail_count"], 1)
            self.assertEqual(resolver.last_status, "retry")

    def test_avito_transport_has_persistent_cookie_jar(self):
        watcher = appmod.Watcher("key", "https://www.avito.ru/moskva", FakeBot())
        self.assertIsNotNone(watcher._client.session.cookies)
        watcher._client.close()

    def test_monitoring_classes_are_reexported_from_entrypoint(self):
        self.assertIs(appmod.Watcher, monitoring.Watcher)
        self.assertIs(appmod.WatcherManager, monitoring.WatcherManager)

    def test_entrypoint_imports_admin_chat_id(self):
        self.assertIn("ADMIN_CHAT_ID", appmod.App.setup_menu.__globals__)

    def test_only_new_skips_primed_ads_but_disabled_mode_delivers(self):
        async def scenario():
            bot = FakeBot()
            bot.app = FakeDeliveryApp()
            watcher = appmod.Watcher("key", "https://www.avito.ru/moskva", bot)
            ad = appmod.Ad(ad_id="1", url="https://www.avito.ru/1", title="Phone")
            watcher.seen[ad.ad_id] = time.time()
            watcher.add_sub(appmod.Subscription(
                id=1, user_id=1, search_key="key", url=watcher.url, only_new=True
            ))
            watcher.add_sub(appmod.Subscription(
                id=2, user_id=2, search_key="key", url=watcher.url, only_new=False
            ))
            watcher._fetch_ads = AsyncMock(return_value=[ad])

            with patch("avito_monitoring.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await watcher._run()

            self.assertEqual(bot.app.deliveries, [2])
            watcher._client.close()

        asyncio.run(scenario())

    def test_only_new_requires_exact_time_and_accepts_new_promoted_ads(self):
        async def scenario():
            bot = FakeBot()
            bot.app = FakeDeliveryApp()
            watcher = appmod.Watcher("key", "https://www.avito.ru/moskva", bot)
            watcher.add_sub(appmod.Subscription(
                id=1,
                user_id=1,
                search_key="key",
                url=watcher.url,
                only_new=True,
                started_ts=1_000,
            ))
            watcher._fetch_ads = AsyncMock(return_value=[
                appmod.Ad(
                    ad_id="old-promoted",
                    url="https://www.avito.ru/old",
                    title="Old promoted",
                    published_ts=900,
                    published_exact=True,
                    is_promoted=True,
                ),
                appmod.Ad(
                    ad_id="unknown",
                    url="https://www.avito.ru/unknown",
                    title="Unknown time",
                    published_ts=None,
                    published_exact=False,
                ),
                appmod.Ad(
                    ad_id="new-promoted",
                    url="https://www.avito.ru/new",
                    title="New promoted",
                    published_ts=1_005,
                    published_exact=True,
                    is_promoted=True,
                ),
            ])

            with patch.object(monitoring, "START_STRICT", True), patch(
                "avito_monitoring.asyncio.sleep",
                AsyncMock(side_effect=asyncio.CancelledError),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await watcher._run()

            self.assertEqual(bot.app.user_sent, {(1, "new-promoted")})
            watcher._client.close()

        asyncio.run(scenario())

    def test_only_new_rejects_bumped_old_listing_by_id_frontier(self):
        # Avito stamps sortTimeStamp/allowTimeStamp = now when an old listing is
        # bumped, so it looks fresh by time. The monotonic item-ID is the only
        # tell: an ID below the freshness frontier is a re-surfaced old ad.
        async def scenario():
            bot = FakeBot()
            bot.app = FakeDeliveryApp()
            watcher = appmod.Watcher("key", "https://www.avito.ru/moskva", bot)
            watcher._id_frontier = 8_400_000_000
            watcher.add_sub(appmod.Subscription(
                id=1, user_id=1, search_key="key", url=watcher.url,
                only_new=True, started_ts=1_000,
            ))
            now_ish = time.time()
            watcher._fetch_ads = AsyncMock(return_value=[
                appmod.Ad(  # bumped: 5 weeks older ID, but timestamp says "now"
                    ad_id="8200000001", url="https://www.avito.ru/8200000001",
                    title="Bumped old", published_ts=now_ish, published_exact=True,
                ),
                appmod.Ad(  # genuinely new: ID above the frontier
                    ad_id="8400000123", url="https://www.avito.ru/8400000123",
                    title="Truly new", published_ts=now_ish, published_exact=True,
                ),
            ])

            with patch.object(monitoring, "START_STRICT", True), patch.object(
                monitoring, "ONLY_NEW_ID_GATE", True
            ), patch(
                "avito_monitoring.asyncio.sleep",
                AsyncMock(side_effect=asyncio.CancelledError),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await watcher._run()

            self.assertEqual(bot.app.user_sent, {(1, "8400000123")})
            self.assertEqual(watcher._id_frontier, 8_400_000_123)
            watcher._client.close()

        asyncio.run(scenario())

    def test_same_ad_reaches_every_subscribed_user_once(self):
        # Dedup is per user: two users watching the same search each get the ad
        # exactly once — one user's delivery never suppresses another's.
        async def scenario():
            bot = FakeBot()
            bot.app = FakeDeliveryApp()
            watcher = appmod.Watcher("key", "https://www.avito.ru/moskva", bot)
            ad = appmod.Ad(ad_id="1", url="https://www.avito.ru/1", title="Phone")
            watcher.add_sub(appmod.Subscription(
                id=1, user_id=1, search_key="key", url=watcher.url, only_new=False
            ))
            watcher.add_sub(appmod.Subscription(
                id=2, user_id=2, search_key="key", url=watcher.url, only_new=False
            ))
            watcher._fetch_ads = AsyncMock(return_value=[ad])

            with patch("avito_monitoring.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await watcher._run()

            self.assertEqual(sorted(bot.app.deliveries), [1, 2])
            self.assertEqual(bot.app.user_sent, {(1, "1"), (2, "1")})
            watcher._client.close()

        asyncio.run(scenario())

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

    def test_pow_solver_finds_nonce_for_known_complexity(self):
        nonce = avito_pow.solve_nonce("test-id", 1)
        digest = hashlib.sha256(f"test-id:{nonce}".encode()).hexdigest()
        self.assertTrue(digest.startswith("0"))
        nonce4 = avito_pow.solve_nonce("test-id-4", 4)
        digest4 = hashlib.sha256(f"test-id-4:{nonce4}".encode()).hexdigest()
        self.assertTrue(digest4.startswith("0000"))

    def test_pow_b64url_jwt_payload(self):
        payload = {"id": "abc", "compl": 4}
        segment = base64.urlsafe_b64encode(
            json.dumps(payload).encode()
        ).decode().rstrip("=")
        self.assertEqual(avito_pow._b64url_json(segment), payload)

    def test_get_items_solves_pow_and_retries(self):
        # 439 с pow_challenge -> PoW решён -> повторный GET даёт 200 JSON.
        challenge_jwt_payload = {"id": "pow-id", "compl": 1}
        jwt_segment = base64.urlsafe_b64encode(
            json.dumps(challenge_jwt_payload).encode()
        ).decode().rstrip("=")
        challenge_jwt = f"header.{jwt_segment}.signature"

        blocked_response = MagicMock(
            status_code=439,
            headers={"Content-Type": "text/html", "set-cookie": "pow_challenge=abc123; Path=/"},
            text="Доступ ограничен: проверка безопасности",
        )
        ok_response = MagicMock(
            status_code=200,
            headers={"Content-Type": "application/json"},
            text='{"catalog": {"items": [{"id": 1}]}}',
        )

        client = avito_api.AvitoHttpClient(timeout=1)
        client.warmed_at = time.time()
        client._session.get = MagicMock(side_effect=[blocked_response, ok_response])
        client._session.post = MagicMock(
            side_effect=[
                MagicMock(status_code=200, json=lambda: {"success": {"result": {"challenge_jwt": challenge_jwt}}}),
                MagicMock(status_code=200, json=lambda: {"success": {"result": {"verified": True}}}),
            ]
        )
        client._session.cookies = MagicMock()
        client._session.cookies.__iter__ = MagicMock(return_value=iter([]))
        client._session.cookies.get = MagicMock(side_effect=lambda k, default=None: {
            "pow_challenge": "abc123",
        }.get(k, default))

        data = client.get_items("https://www.avito.ru/web/1/js/items?q=test")
        self.assertEqual(len(data["catalog"]["items"]), 1)
        self.assertEqual(client._session.get.call_count, 2)
        self.assertEqual(client._session.post.call_count, 2)
        self.assertEqual(getattr(client, "total_pow_solved", 0), 1)
        client.close()

    def test_get_items_raises_when_pow_not_verified(self):
        # verify вернул verified=False -> блок пробрасывается наружу.
        challenge_jwt_payload = {"id": "pow-id", "compl": 1}
        jwt_segment = base64.urlsafe_b64encode(
            json.dumps(challenge_jwt_payload).encode()
        ).decode().rstrip("=")
        challenge_jwt = f"header.{jwt_segment}.signature"

        blocked_response = MagicMock(
            status_code=439,
            headers={"Content-Type": "text/html", "set-cookie": "pow_challenge=abc123; Path=/"},
            text="Доступ ограничен: проверка безопасности",
        )

        client = avito_api.AvitoHttpClient(timeout=1)
        client.warmed_at = time.time()
        client._session.get = MagicMock(return_value=blocked_response)
        client._session.post = MagicMock(
            side_effect=[
                MagicMock(status_code=200, json=lambda: {"success": {"result": {"challenge_jwt": challenge_jwt}}}),
                MagicMock(status_code=200, json=lambda: {"success": {"result": {"verified": False}}}),
                MagicMock(status_code=200, json=lambda: {"success": {"result": {"challenge_jwt": challenge_jwt}}}),
                MagicMock(status_code=200, json=lambda: {"success": {"result": {"verified": False}}}),
            ]
        )
        client._session.cookies = MagicMock()
        client._session.cookies.__iter__ = MagicMock(return_value=iter([]))
        client._session.cookies.get = MagicMock(side_effect=lambda k, default=None: {
            "pow_challenge": "abc123",
        }.get(k, default))

        with self.assertRaises(avito_api.AvitoBlock) as raised:
            client.get_items("https://www.avito.ru/web/1/js/items?q=test")
        self.assertEqual(raised.exception.kind, "challenge")
        client.close()

    def test_cookie_store_roundtrip(self):
        # Cookies сохраняются в store и восстанавливаются в новом клиенте.
        with tempfile.TemporaryDirectory() as tmp:
            store = str(Path(tmp) / "cookies_direct.json")
            first = avito_api.AvitoHttpClient(timeout=1, cookie_store=store)
            first._session.cookies.set("srv_id", "x" * 40, domain=".avito.ru", path="/")
            first._session.cookies.set("u", "y" * 20, domain=".avito.ru", path="/")
            saved = first.save_cookies()
            self.assertEqual(saved, 2)
            self.assertTrue(Path(store).exists())
            first.close()

            second = avito_api.AvitoHttpClient(timeout=1, cookie_store=store)
            self.assertEqual(second.cookies_loaded, 2)
            self.assertEqual(second._session.cookies.get("srv_id"), "x" * 40)
            self.assertEqual(second._session.cookies.get("u"), "y" * 20)
            second.close()

    def test_cookie_store_skips_expired(self):
        # Истёкшие cookies не восстанавливаются.
        with tempfile.TemporaryDirectory() as tmp:
            store = str(Path(tmp) / "cookies_direct.json")
            entries = [
                {"name": "dead", "value": "v", "domain": ".avito.ru", "path": "/",
                 "expires": time.time() - 100},
                {"name": "alive", "value": "w", "domain": ".avito.ru", "path": "/",
                 "expires": time.time() + 3600},
            ]
            Path(store).write_text(json.dumps(entries), encoding="utf-8")
            client = avito_api.AvitoHttpClient(timeout=1, cookie_store=store)
            self.assertEqual(client.cookies_loaded, 1)
            self.assertIsNone(client._session.cookies.get("dead"))
            self.assertEqual(client._session.cookies.get("alive"), "w")
            client.close()

    def test_warmup_skipped_when_cookies_restored(self):
        # >=3 восстановленных cookies -> warmup сразу возвращает True без запроса.
        with tempfile.TemporaryDirectory() as tmp:
            store = str(Path(tmp) / "cookies_direct.json")
            entries = [
                {"name": f"c{i}", "value": f"v{i}", "domain": ".avito.ru", "path": "/",
                 "expires": time.time() + 3600}
                for i in range(3)
            ]
            Path(store).write_text(json.dumps(entries), encoding="utf-8")
            client = avito_api.AvitoHttpClient(timeout=1, cookie_store=store)
            client._session.get = MagicMock()
            self.assertTrue(client.warmup())
            client._session.get.assert_not_called()
            client.close()

    def test_reset_keeps_cookie_store(self):
        # reset() пересоздаёт сессию, но cookies из store возвращаются.
        with tempfile.TemporaryDirectory() as tmp:
            store = str(Path(tmp) / "cookies_direct.json")
            entries = [
                {"name": "keep", "value": "kept", "domain": ".avito.ru", "path": "/",
                 "expires": time.time() + 3600},
            ]
            Path(store).write_text(json.dumps(entries), encoding="utf-8")
            client = avito_api.AvitoHttpClient(timeout=1, cookie_store=store)
            client.reset()
            self.assertEqual(client._session.cookies.get("keep"), "kept")
            client.close()

    def test_sitemap_index_decodes_item_maps(self):
        # Из index.xml достаём item_<slug>_<catId>_<block>.xml.gz-карты.
        locs = [
            "https://www.avito.ru/sitemap/site/canonical_serp_telefony_84_0.xml.gz",
            "https://www.avito.ru/sitemap/site/item_telefony_84_0.xml.gz",
            "https://www.avito.ru/sitemap/site/item_telefony_84_16.xml.gz",
            "https://www.avito.ru/sitemap/site/item_noutbuki_10_1.xml.gz",
        ]
        maps = avito_sitemap._decode_index(locs)
        self.assertEqual(len(maps), 3)
        telefony = [m for m in maps if m.category_slug == "telefony"]
        self.assertEqual({m.block for m in telefony}, {0, 16})
        self.assertEqual(telefony[0].category_id, 84)
        noutbuki = [m for m in maps if m.category_slug == "noutbuki"]
        self.assertEqual((noutbuki[0].category_id, noutbuki[0].block), (10, 1))

    def test_sitemap_latest_block_per_category(self):
        with patch.object(avito_sitemap, "fetch_index", return_value=[
            avito_sitemap.ItemSitemap("telefony", 84, 0, "u0"),
            avito_sitemap.ItemSitemap("telefony", 84, 16, "u16"),
            avito_sitemap.ItemSitemap("telefony", 84, 7, "u7"),
            avito_sitemap.ItemSitemap("noutbuki", 10, 2, "n2"),
        ]):
            latest = avito_sitemap.latest_item_sitemaps()
            self.assertEqual(len(latest), 2)
            by_slug = {m.category_slug: m for m in latest}
            self.assertEqual(by_slug["telefony"].block, 16)
            self.assertEqual(by_slug["noutbuki"].block, 2)
            only_tel = avito_sitemap.latest_item_sitemaps(category_slug="telefony")
            self.assertEqual(len(only_tel), 1)
            self.assertEqual(only_tel[0].block, 16)

    def test_sitemap_mark_seen_extracts_ids(self):
        # item-ID (числовой суффикс URL) попадает в seen.
        items = [
            ("https://www.avito.ru/irkutsk/telefony/chasy_apple_watch_8_45_mm_8330204224", "2026-08-27T03:24:45Z"),
            ("https://www.avito.ru/omsk/telefony/infinix_gt_30_pro_8256_gb_2_sim_8385803323", "2026-08-27T03:24:48Z"),
        ]
        with patch.object(avito_sitemap, "fetch_fresh_items", return_value=items):
            seen = {}
            marked = avito_sitemap.mark_seen_from_sitemap("telefony", 84, seen)
        self.assertEqual(marked, 2)
        self.assertIn("8330204224", seen)
        self.assertIn("8385803323", seen)

    def test_sitemap_mark_seen_swallows_errors(self):
        # Сетевая ошибка в sitemap-канале не ломает Watcher.
        with patch.object(avito_sitemap, "fetch_fresh_items", side_effect=OSError("boom")):
            seen = {}
            marked = avito_sitemap.mark_seen_from_sitemap("telefony", 84, seen)
        self.assertEqual(marked, 0)
        self.assertEqual(seen, {})

    def test_watcher_marks_seen_from_sitemap_on_ip_block(self):
        # При ip_block Watcher вызывает sitemap-пометку (best-effort seen).
        appmod.Watcher._route_blocked_until.clear()
        watcher = appmod.Watcher("key", "https://www.avito.ru/moskva/telefony?q=iphone", FakeBot())
        watcher._api_url = "https://www.avito.ru/web/1/js/items?categoryId=84&q=iphone"
        watcher._client = MagicMock()
        watcher._client.get_search_page_items = MagicMock(
            side_effect=avito_api.AvitoBlock("ip_block", 429)
        )
        watcher._client.get_items = MagicMock(
            side_effect=avito_api.AvitoBlock("ip_block", 429)
        )
        watcher._proxy = lambda: None
        watcher._rotate_proxy = lambda: False
        marked_urls = [
            ("https://www.avito.ru/moskva/telefony/iphone_16_8323342236", "2026-08-27T03:25:04Z"),
        ]
        watcher._mark_seen_from_sitemap = AsyncMock(
            side_effect=lambda: avito_sitemap.mark_seen_from_sitemap(
                *(watcher._sitemap_category() + (watcher.seen,))
            )
        )
        with patch.object(avito_sitemap, "fetch_fresh_items", return_value=marked_urls):
            ads = asyncio.run(watcher._fetch_ads())
        self.assertIsNone(ads)
        self.assertEqual(watcher.last_block_kind, "ip_block")
        watcher._mark_seen_from_sitemap.assert_awaited_once()
        self.assertIn("8323342236", watcher.seen)

    def test_watcher_sitemap_category_parsed(self):
        watcher = appmod.Watcher("key", "https://www.avito.ru/moskva/telefony?q=iphone", FakeBot())
        watcher._api_url = "https://www.avito.ru/web/1/js/items?categoryId=84&q=iphone"
        slug, cat_id = watcher._sitemap_category()
        self.assertEqual(cat_id, 84)
        self.assertEqual(slug, "telefony")

    def test_ip_block_engages_parole_mode(self):
        # ip_block включает parole: первые PAROLE_POLLS пауз длиннее в PAROLE_FACTOR.
        watcher = appmod.Watcher("key", "https://www.avito.ru/moskva/telefony", FakeBot())
        watcher._interval = 30.0
        self.assertEqual(watcher._parole_polls_left, 0)
        normal = watcher._poll_delay()
        self.assertLess(normal, 45.0)

        watcher._parole_polls_left = monitoring.PAROLE_POLLS
        for expected_left in (monitoring.PAROLE_POLLS - 1, monitoring.PAROLE_POLLS - 2, 0):
            parole = watcher._poll_delay()
            self.assertGreaterEqual(parole, 30.0 * 0.9 * monitoring.PAROLE_FACTOR)
            self.assertEqual(watcher._parole_polls_left, expected_left)
        # Parole исчерпан — снова обычные паузы.
        after = watcher._poll_delay()
        self.assertLess(after, 45.0)

    def test_proxy_harvest_dedup_and_parse(self):
        # harvest(): дедуп по url между источниками + парсинг обоих форматов.
        with patch.object(proxy_harvest, "_fetch_text", side_effect=[
            json.dumps({"data": [
                {"ip": "1.2.3.4", "port": 8080, "asn": "AS1", "org": "Test Org", "anonymityLevel": "elite"},
            ]}),                                   # geonode
            "5.6.7.8:3128\n\n1.2.3.4:8080\n",      # proxyscrape (дубль должен уйти)
            "7.7.7.7:80\n",                        # proxifly
        ]):
            candidates = proxy_harvest.harvest()
        urls = [c.url for c in candidates]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual(set(urls), {"http://1.2.3.4:8080", "http://5.6.7.8:3128", "http://7.7.7.7:80"})
        geo = [c for c in candidates if c.source == "geonode"][0]
        self.assertEqual(geo.asn, "AS1")

    def test_proxy_harvest_check_and_export(self):
        # Проверка одного кандидата: connect/status/ms; экспорт строки env.
        cand = proxy_harvest.ProxyCandidate(url="http://9.9.9.9:80", source="test")
        fake_response = MagicMock(status_code=200, content=b"x" * 100)
        fake_session = MagicMock()
        fake_session.get.return_value = fake_response
        with patch.object(proxy_harvest.curl_requests, "Session", return_value=fake_session):
            result = proxy_harvest.check_proxy(cand)
        self.assertTrue(result["connect"])
        self.assertEqual(result["status"], 200)

        report = proxy_harvest.HarvestReport(target="t")
        report.harvested = 2
        report.by_source = {"test": 2}
        report.connected = 1
        report.passed = 1
        report.statuses = {200: 1, 403: 0}
        report.working = [result]
        env = proxy_harvest.export_env_string(report)
        self.assertEqual(env, "http://9.9.9.9:80")
        self.assertIn("собрано 2", report.summary())

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
            watcher._consecutive_blocks = 2
            watcher.last_block_kind = "ip_block"
            watcher._client.last_status = 200
            watcher._client.get_search_page_items = MagicMock(return_value=[{
                "ad_id": "1",
                "url": "https://www.avito.ru/moskva/x_1234567",
                "title": "Phone",
            }])
            watcher._client.get_items = MagicMock()
            with patch.object(watcher, "_wait_global_rate_limit", AsyncMock()):
                ads = await watcher._fetch_ads()
            self.assertEqual([ad.ad_id for ad in ads], ["1"])
            self.assertEqual(watcher._consecutive_blocks, 0)
            self.assertIsNone(watcher.last_block_kind)
            watcher._client.get_items.assert_not_called()
            watcher._client.close()
        asyncio.run(scenario())

    def test_html_failure_uses_managed_api_fallback(self):
        async def scenario():
            watcher = appmod.Watcher("key", "https://www.avito.ru/moskva", FakeBot())
            watcher._api_url = "https://www.avito.ru/web/1/js/items?q=test&sort=date"
            watcher._api_route_managed = True
            watcher._client.get_search_page_items = MagicMock(
                side_effect=avito_api.AvitoBlock("challenge", 439)
            )
            watcher._client.get_items = MagicMock(return_value={"catalog": {"items": [{
                "id": 2,
                "urlPath": "/moskva/x_2345678",
                "title": "API fallback",
            }]}})
            with patch.object(watcher, "_wait_global_rate_limit", AsyncMock()):
                ads = await watcher._fetch_ads()
            self.assertEqual([ad.ad_id for ad in ads], ["2"])
            self.assertIsNotNone(watcher._api_url)
            watcher._client.close()

        asyncio.run(scenario())

    def test_html_polling_does_not_resolve_or_call_api(self):
        async def scenario():
            resolver = MagicMock()
            resolver.resolve.return_value = (
                "https://www.avito.ru/web/1/js/items?q=test&sort=date"
            )
            watcher = appmod.Watcher(
                "key", "https://www.avito.ru/moskva", FakeBot(), route_resolver=resolver
            )
            watcher._client.get_search_page_items = MagicMock(return_value=[{
                "ad_id": "1",
                "url": "https://www.avito.ru/moskva/x_1234567",
                "title": "HTML",
            }])
            watcher._client.get_items = MagicMock()
            with patch.object(watcher, "_wait_global_rate_limit", AsyncMock()):
                ads = await watcher._fetch_ads()
            self.assertEqual([ad.ad_id for ad in ads], ["1"])
            self.assertIsNone(watcher._api_url)
            resolver.resolve.assert_not_called()
            watcher._client.get_items.assert_not_called()
            watcher._client.close()

        asyncio.run(scenario())

    def test_api_route_parses_up_to_fifty_items(self):
        async def scenario():
            watcher = appmod.Watcher("key", "https://www.avito.ru/moskva", FakeBot())
            watcher._api_url = "https://www.avito.ru/web/1/js/items?q=test&sort=date"
            watcher._client.get_search_page_items = MagicMock(return_value=[])
            items = [{
                "id": index,
                "urlPath": f"/moskva/x_{1234567 + index}",
                "title": f"Phone {index}",
            } for index in range(55)]
            watcher._client.get_items = MagicMock(return_value={"catalog": {"items": items}})
            with patch.object(watcher, "_wait_global_rate_limit", AsyncMock()):
                ads = await watcher._fetch_ads()
            self.assertEqual(len(ads), 50)
            watcher._client.close()

        asyncio.run(scenario())

    def test_fetch_ads_invalidates_schema_mismatch(self):
        async def scenario():
            appmod.Watcher._route_blocked_until.clear()
            watcher = appmod.Watcher("key", "https://www.avito.ru/moskva", FakeBot())
            watcher._api_url = "https://www.avito.ru/web/1/js/items?q=test"
            watcher._client.last_status = 200
            watcher._client.get_items = MagicMock(return_value={"unexpected": []})
            watcher._client.get_search_page_items = MagicMock(return_value=[])
            with patch.object(watcher, "_wait_global_rate_limit", AsyncMock()), \
                    patch.object(watcher.route_resolver, "invalidate") as invalidate:
                self.assertIsNone(await watcher._fetch_ads())
            self.assertEqual(watcher.parser_health, "schema_mismatch")
            self.assertEqual(watcher.conversion_status, "retry")
            self.assertIsNone(watcher._api_url)
            invalidate.assert_called_once_with(
                watcher.url, "API schema mismatch: items missing"
            )
            watcher._client.close()
            appmod.Watcher._route_blocked_until.clear()

        asyncio.run(scenario())

    def test_fetch_ads_block_applies_cooldown_and_resets_session(self):
        async def scenario():
            appmod.Watcher._route_blocked_until.clear()
            watcher = appmod.Watcher("key", "https://www.avito.ru/moskva", FakeBot())
            watcher._api_url = "https://www.avito.ru/web/1/js/items?q=test"
            watcher._client.get_search_page_items = MagicMock(return_value=[])
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
            first._client.get_search_page_items = MagicMock(return_value=[])
            second._client.get_search_page_items = MagicMock(return_value=[])
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
                watcher._client.get_search_page_items = MagicMock(return_value=[])
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
            watcher._api_route_managed = True
            watcher._client.get_search_page_items = MagicMock(return_value=[])
            watcher._client.get_items = MagicMock(side_effect=avito_api.AvitoHttpError(404, "gone"))
            with patch.object(watcher, "_wait_global_rate_limit", AsyncMock()), \
                    patch.object(watcher.route_resolver, "invalidate") as invalidate:
                self.assertIsNone(await watcher._fetch_ads())
            self.assertIsNone(watcher._api_url)
            invalidate.assert_called_once_with(watcher.url, "http 404: gone")
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

    def test_equivalent_urls_share_watcher_and_search_key(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                manager = appmod.WatcherManager(FakeBot(), str(Path(tmp) / "subs.json"))
                with patch.object(appmod.Watcher, "start", AsyncMock()):
                    first = await manager.add_subscription(
                        7,
                        "http://www.avito.ru/moskva?b=2&a=1&utm_source=test",
                    )
                    second = await manager.add_subscription(
                        8,
                        "https://avito.ru/moskva?a=1&b=2",
                    )
                self.assertEqual(first.search_key, second.search_key)
                self.assertTrue(first.original_url.startswith("http://www.avito.ru"))
                self.assertEqual(len(manager.watchers), 1)
                self.assertEqual(len(manager.watchers[first.search_key].subscribers), 2)

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

    def test_restore_uses_original_url_fallback_and_keeps_filters(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                path = str(Path(tmp) / "subs.json")
                update_json(
                    path,
                    [],
                    lambda rows: rows.append(
                        {
                            "id": 4,
                            "user_id": 9,
                            "url": "not-a-url",
                            "search_key": "also-invalid",
                            "original_url": "http://www.avito.ru/moskva?q=test",
                            "filter": {"keywords_any": ["Galaxy"], "price_max": 50000},
                        }
                    ),
                )
                manager = appmod.WatcherManager(FakeBot(), path)
                with patch.object(appmod.Watcher, "start", AsyncMock()):
                    await manager.restore()
                restored = manager.list_user_subs(9)[0]
                self.assertEqual(restored.url, "https://avito.ru/moskva?q=test")
                self.assertEqual(restored.flt.keywords_any, ["Galaxy"])
                self.assertEqual(restored.flt.price_max, 50000)

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

    def test_sqlite_state_updates_are_transactional(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "state.sqlite3")
            with patch.dict(os.environ, {"STORAGE_BACKEND": "sqlite", "DATABASE_FILE": db_path}):
                update_state("sqlite_test_accounts.json", {}, lambda data: data.__setitem__("first", 1))
                update_state("sqlite_test_accounts.json", {}, lambda data: data.__setitem__("second", 2))
                self.assertEqual(load_state("sqlite_test_accounts.json", {}), {"first": 1, "second": 2})
                self.assertTrue(Path(db_path).exists())

    def test_legacy_api_cache_imports_to_sqlite_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "state.sqlite3")
            previous = Path.cwd()
            os.chdir(tmp)
            try:
                Path("api_urls.json").write_text(
                    json.dumps({"https://avito.ru/moskva": "https://avito.ru/web/1/js/items"}),
                    encoding="utf-8",
                )
                with patch.dict(
                    os.environ,
                    {"STORAGE_BACKEND": "sqlite", "DATABASE_FILE": db_path},
                ):
                    imported = load_state("api_urls.json", {})
                    Path("api_urls.json").write_text("{}", encoding="utf-8")
                    self.assertEqual(load_state("api_urls.json", {}), imported)
                self.assertTrue(Path("api_urls.json").exists())
            finally:
                os.chdir(previous)

    def test_account_service_redeems_key_once_and_restores_license(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = AccountService(
                LicenseManager(),
                accounts_file=str(Path(tmp) / "accounts.json"),
                keys_file=str(Path(tmp) / "keys.json"),
            )
            key = service.issue_key(hours=1)
            redeemed = service.redeem_key(key)
            self.assertIsNotNone(redeemed)
            self.assertIsNone(service.redeem_key(key))
            service.register_if_needed(42)
            service.add_key(42, key, redeemed[0], redeemed[1])
            restored = AccountService(
                LicenseManager(),
                accounts_file=service.accounts_file,
                keys_file=service.keys_file,
            )
            restored._restore_licenses()
            self.assertTrue(restored.license.is_active(42))

    def test_alert_binding_token_is_one_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "links.json")
            with patch.object(alert_bot, "ALERT_LINKS_FILE", path):
                update_json(path, {}, lambda data: data.__setitem__(
                    "secret", {"user_id": 777, "expires": time.time() + 60}))
                self.assertEqual(alert_bot._consume_link_token("secret"), 777)
                self.assertIsNone(alert_bot._consume_link_token("secret"))
                self.assertIsNone(alert_bot._consume_link_token("777"))

    def test_alert_keyboard_uses_styles_copy_text_and_custom_emoji(self):
        with patch.dict(avito_ui._BUTTON_ICON_IDS, {"primary": "emoji-1"}):
            markup = avito_ui._alert_reply_markup(
                "<b>Samsung</b>\nhttps://www.avito.ru/123456789"
            )

        self.assertIsNotNone(markup)
        open_button, copy_button = (row[0] for row in markup["inline_keyboard"])
        self.assertEqual(open_button["style"], "primary")
        self.assertEqual(open_button["icon_custom_emoji_id"], "emoji-1")
        self.assertEqual(open_button["url"], "https://www.avito.ru/123456789")
        self.assertEqual(copy_button["style"], "success")
        self.assertEqual(
            copy_button["copy_text"]["text"], "https://www.avito.ru/123456789"
        )

    def test_alert_message_effect_retries_without_effect(self):
        class FakeResponse:
            def __init__(self, status, body):
                self.status = status
                self.body = body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def json(self, content_type=None):
                return self.body

        class FakeSession:
            closed = False

            def __init__(self):
                self.calls = []
                self.responses = [
                    FakeResponse(400, {"ok": False, "description": "bad effect"}),
                    FakeResponse(200, {"ok": True}),
                ]

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return self.responses.pop(0)

        async def scenario():
            app = object.__new__(avito_ui.App)
            app.alert_token = "token"
            app.alert_message_effect_id = "effect-1"
            app._alert_session = FakeSession()

            sent = await app.send_to_alert(
                42,
                "<b>Samsung</b>\nhttps://www.avito.ru/123456789",
                None,
            )

            self.assertTrue(sent)
            first_payload = app._alert_session.calls[0][1]["json"]
            second_payload = app._alert_session.calls[1][1]["json"]
            self.assertEqual(first_payload["message_effect_id"], "effect-1")
            self.assertNotIn("message_effect_id", second_payload)
            self.assertIn("reply_markup", second_payload)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
