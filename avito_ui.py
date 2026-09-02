# -*- coding: utf-8 -*-
"""
Avito Monitor Bot (aiogram 3.x)
— v5.0 (API-first) — ИСПРАВЛЕННАЯ ВЕРСИЯ
    • Переход на JSON API Авито (web/1/js/items) вместо мёртвого HTML-парсера
    • Классификация блокировок Qrator (403 rate_limit, 429 ip_block, 439 challenge)
    • Прогрев сессии + браузерные заголовки
    • Умный бэкофф с Retry-After + экспонентой
    • Удобное управление (инлайн-меню + статусы)
    • Легко добавлять прокси со сменой IP
    • Rerun `python test_regressions.py` — все тесты должны пройти
"""

import asyncio
import html
import json
import logging
import os
import re
import secrets
import time
import uuid
from typing import Any, Dict, List, Optional, Set, cast

import aiohttp
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from avito_accounts import AccountService
from avito_api import parse_api_items as parse_api_items
from avito_domain import (
    KEY_RE,
    LicenseManager,
    SearchSpec,
    SubscriberFilter,
    Subscription,
    _br,
    _fmt_dt,
    _parse_price_input,
    avito_short_url,
    extract_url_metadata,
    normalize_filter,
    parse_avito_url,
)
from avito_domain import (
    Ad as Ad,
)
from avito_domain import (
    FeedItem as FeedItem,
)
from avito_domain import (
    _attr_to_str as _attr_to_str,
)
from avito_domain import (
    _extract_ad_id as _extract_ad_id,
)
from avito_domain import (
    _get_text as _get_text,
)
from avito_domain import (
    is_valid_avito_url as is_valid_avito_url,
)
from avito_domain import (
    search_key_from_url as search_key_from_url,
)
from avito_domain import (
    try_extract_filters_from_url as try_extract_filters_from_url,
)
from avito_monitoring import Watcher, WatcherManager
from avito_settings import (
    ACCOUNTS_FILE,
    ADMIN_CHAT_ID,
    ALERT_BOT_TOKEN,
    ALERT_BOT_USERNAME,
    ALERT_LINKS_FILE,
    ALERT_MESSAGE_EFFECT_ID,
    BINDINGS_FILE,
    DEDUP_TTL_DAYS,
    KEYS_FILE,
    SENT_FILE,
    SUPPORT_LINK,
    TELEGRAM_DANGER_BUTTON_ICON_ID,
    TELEGRAM_PRIMARY_BUTTON_ICON_ID,
    TELEGRAM_SUCCESS_BUTTON_ICON_ID,
)
from avito_settings import (
    API_URLS_FILE as API_URLS_FILE,
)
from avito_settings import (
    AVITO_PROXIES as AVITO_PROXIES,
)
from avito_settings import (
    AVITO_PROXY_CHANGE_URLS as AVITO_PROXY_CHANGE_URLS,
)
from avito_settings import (
    SUBSCRIPTIONS_FILE as SUBSCRIPTIONS_FILE,
)
from storage import load_state, save_state, update_state

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

_BUTTON_ICON_IDS = {
    "primary": TELEGRAM_PRIMARY_BUTTON_ICON_ID,
    "success": TELEGRAM_SUCCESS_BUTTON_ICON_ID,
    "danger": TELEGRAM_DANGER_BUTTON_ICON_ID,
}


def _inline_button(*, text: str, style: Optional[str] = None, **kwargs: Any):
    style = style or "primary"
    icon_id = _BUTTON_ICON_IDS.get(style)
    return types.InlineKeyboardButton(
        text=text,
        style=style,
        icon_custom_emoji_id=icon_id,
        **kwargs,
    )


def _keyboard_button(*, text: str, style: Optional[str] = None, **kwargs: Any):
    style = style or "primary"
    icon_id = _BUTTON_ICON_IDS.get(style)
    return types.KeyboardButton(
        text=text,
        style=style,
        icon_custom_emoji_id=icon_id,
        **kwargs,
    )


# === Главное меню ===
MAIN_INLINE_KB = types.InlineKeyboardMarkup(
    inline_keyboard=[
        [
            _inline_button(
                text="🔎 Мои поиски", callback_data="searches", style="primary"
            ),
            _inline_button(
                text="👤 Аккаунт", callback_data="account", style="primary"
            ),
        ],
        [
            _inline_button(text="ℹ️ Помощь", callback_data="help", style="primary"),
            _inline_button(text="Поддержка", callback_data="support", style="primary"),
        ],
    ],
)

MAIN_MENU_TEXT = (
    "<b>Мониторинг Avito</b>\nНовые объявления приходят в отдельный бот-оповещатель."
)

HELP_TEXT = (
    "<b>Как начать</b>\n\n"
    "1. Активируйте ключ доступа.\n"
    "2. Подключите бот-оповещатель.\n"
    "3. Создайте поиск и укажите фильтры.\n\n"
    "Управление поисками доступно в разделе «Мои поиски»."
)


def build_help_content(
    app: Optional[Any], user_id: int
) -> tuple[str, types.InlineKeyboardMarkup]:
    alert_chat_id = (
        app.get_alert_chat_id(user_id)
        if app and hasattr(app, "get_alert_chat_id")
        else None
    )
    alert_username = (
        getattr(app, "alert_username", ALERT_BOT_USERNAME) or ALERT_BOT_USERNAME
    )

    if alert_chat_id:
        status_line = "🔔 <b>Бот оповещений:</b> ✅ Подключен"
        alert_btn_text = "🔔 Открыть бот оповещений"
        alert_url = f"https://t.me/{alert_username}" if alert_username else ""
    else:
        status_line = "🔔 <b>Бот оповещений:</b> ❌ Не подключен"
        alert_btn_text = "🔔 Подключить оповещения"
        alert_url = (
            app.alert_deeplink(user_id)
            if app and hasattr(app, "alert_deeplink")
            else (f"https://t.me/{alert_username}" if alert_username else "")
        )

    text = (
        "<b>Как начать пользоваться</b>\n\n"
        "1. Активируйте ключ доступа (отправьте ключ в чат).\n"
        "2. Подключите бот-оповещатель.\n"
        "3. Создайте поиск (отправьте ссылку с Avito).\n\n"
        f"{status_line}\n\n"
        "Новые объявления будут мгновенно приходить в отдельный бот-оповещатель."
    )

    rows: List[List[types.InlineKeyboardButton]] = []
    if alert_url:
        rows.append(
            [
                _inline_button(
                    text=alert_btn_text,
                    url=alert_url,
                    style="success" if not alert_chat_id else "primary",
                )
            ]
        )
    rows.append(
        [
            _inline_button(text="Написать в поддержку", url=SUPPORT_LINK),
            _inline_button(text="Назад в меню", callback_data="main_menu"),
        ]
    )

    return text, types.InlineKeyboardMarkup(inline_keyboard=rows)


async def show_main_menu(
    message: types.Message,
    text: str = MAIN_MENU_TEXT,
    *,
    clear_reply_keyboard: bool = False,
) -> None:
    if clear_reply_keyboard:
        sent = await message.answer(text, reply_markup=types.ReplyKeyboardRemove())
        try:
            await sent.edit_reply_markup(reply_markup=MAIN_INLINE_KB)
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=MAIN_INLINE_KB)


# ===== Мастер добавления поиска =====
class SearchWizard(StatesGroup):
    url = State()
    confirm = State()
    price_min = State()
    price_max = State()
    words = State()
    name = State()


wizard_router = Router(name="wizard")

_AVITO_URL_IN_TEXT_RE = re.compile(
    r"(?:(?:https?://(?:[a-z0-9-]+\.)*avito\.ru)|(?<!://)(?:[a-z0-9-]+\.)*avito\.ru)\S*",
    re.I,
)


def _extract_avito_url_text(text: str) -> Optional[str]:
    match = _AVITO_URL_IN_TEXT_RE.search(text or "")
    return match.group(0) if match else None


def _alert_reply_markup(caption: str) -> Optional[Dict[str, Any]]:
    url = _extract_avito_url_text(html.unescape(caption or ""))
    if not url:
        return None
    url = url.rstrip(".,;:!?)]}'\"")
    rows = [
        [
            _inline_button(
                text="Открыть на Avito",
                url=url,
                style="primary",
            )
        ]
    ]
    if len(url) <= 256:
        rows.append(
            [
                _inline_button(
                    text="Скопировать ссылку",
                    copy_text=types.CopyTextButton(text=url),
                    style="success",
                )
            ]
        )
    return types.InlineKeyboardMarkup(inline_keyboard=rows).model_dump(exclude_none=True)


async def _prompt_search_name(
    message: types.Message,
    state: FSMContext,
    url: str,
    pmin: Optional[int],
    pmax: Optional[int],
    keywords: List[str],
) -> None:
    await state.update_data(price_min=pmin, price_max=pmax)
    short_url = html.escape(avito_short_url(url), quote=True)
    min_text = (
        f"{pmin:,}".replace(",", " ") + " ₽" if pmin is not None else "без ограничения"
    )
    max_text = (
        f"{pmax:,}".replace(",", " ") + " ₽" if pmax is not None else "без ограничения"
    )
    data = await state.get_data()
    keywords_any = data.get("keywords_any") or []
    keywords_stop = data.get("keywords_stop") or []
    summary = [
        "<b>Новый поиск · 3 из 3</b>",
        "",
        f"Цена от: {min_text}",
        f"Цена до: {max_text}",
        f"Обязательные слова: {html.escape(', '.join(keywords)) if keywords else 'не заданы'}",
        f"Хотя бы одно: {html.escape(', '.join(keywords_any)) if keywords_any else 'не заданы'}",
        f"Исключить: {html.escape(', '.join(keywords_stop)) if keywords_stop else 'не заданы'}",
        f'<a href="{short_url}">Открыть поиск на Avito</a>',
        "",
        "Отправьте короткое название, например «Samsung до 70 000».",
    ]
    await state.set_state(SearchWizard.name)
    await message.answer(
        _br("\n".join(summary)),
        disable_web_page_preview=True,
        reply_markup=types.ReplyKeyboardRemove(),
    )


def _filter_from_data(data: Dict[str, Any]) -> SubscriberFilter:
    return normalize_filter(
        SubscriberFilter(
            keywords_all=list(data.get("keywords_all", data.get("guessed_kw")) or []),
            keywords_any=list(data.get("keywords_any") or []),
            keywords_stop=list(data.get("keywords_stop") or []),
            price_min=data.get("price_min"),
            price_max=data.get("price_max"),
        )
    )


def _filter_preview(spec: SearchSpec) -> str:
    filters = spec.filters

    def price(value: Optional[int]) -> str:
        return f"{value:,}".replace(",", " ") + " ₽" if value is not None else "не задана"

    warnings = "\n".join(
        f"• {html.escape(warning)}" for warning in spec.warnings
    ) or "нет"

    extra_info: list[str] = []
    if spec.category:
        extra_info.append(f"📁 Категория: <b>{html.escape(spec.category)}</b>")
    if spec.location:
        extra_info.append(f"📍 Регион: <b>{html.escape(spec.location)}</b>")
    if spec.color:
        extra_info.append(f"🎨 Цвет: <b>{html.escape(spec.color)}</b>")
    if spec.delivery:
        extra_info.append("🚚 Доставка: <b>включена</b>")
    if spec.sort_title:
        extra_info.append(f"⏱ Сортировка: <b>{html.escape(spec.sort_title)}</b>")

    extra_str = ("\n" + "\n".join(extra_info)) if extra_info else ""

    return (
        "🔍 <b>Проверка ссылки</b>\n\n"
        f"📌 Тип: <b>{'поиск' if spec.kind == 'search' else 'объявление'}</b>\n"
        f'🔗 <a href="{html.escape(spec.canonical_url, quote=True)}">Нормализованная ссылка</a>\n\n'
        "⚙️ <b>Найденные фильтры</b>\n"
        f"💰 Цена: {price(filters.price_min)} — {price(filters.price_max)}\n"
        f"🔤 Обязательные слова: {html.escape(', '.join(filters.keywords_all)) or 'не заданы'}\n"
        f"🔀 Хотя бы одно: {html.escape(', '.join(filters.keywords_any)) or 'не заданы'}\n"
        f"🚫 Исключить: {html.escape(', '.join(filters.keywords_stop)) or 'не заданы'}"
        f"{extra_str}\n\n"
        f"⚠️ <b>Предупреждения</b>\n{warnings}"
    )


def _conversion_text(watcher: Optional[Watcher]) -> str:
    status = getattr(watcher, "conversion_status", "pending") if watcher else "pending"
    if status == "ready":
        return "готово"
    if status == "retry":
        return "ошибка, повтор запланирован"
    return "ожидает"


async def _continue_after_filter_review(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    filters = _filter_from_data(data)
    if (
        filters.price_min is not None
        and filters.price_max is not None
        and filters.price_min > filters.price_max
    ):
        await state.set_state(SearchWizard.price_min)
        await message.answer(
            "Минимальная цена больше максимальной. Введите корректную минимальную цену или «-»."
        )
        return
    await _prompt_search_name(
        message,
        state,
        str(data.get("url") or ""),
        filters.price_min,
        filters.price_max,
        filters.keywords_all,
    )


@wizard_router.message(Command("newsearch"))
async def wizard_start(message: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(SearchWizard.url)
    await message.answer(
        "<b>Новый поиск · 1 из 3</b>\n\n"
        "Пришлите ссылку на результаты поиска Avito. Все нужные категории и параметры выберите на сайте заранее.",
        reply_markup=types.ReplyKeyboardRemove(),
        disable_web_page_preview=True,
    )


@wizard_router.message(SearchWizard.url, F.text.casefold() == "отмена")
@wizard_router.message(SearchWizard.url, F.text.casefold() == "назад")
@wizard_router.message(
    SearchWizard.url,
    F.text.in_(
        [
            "📘 Инструкция",
            "🛟 Поддержка",
            "🧭 Поиски",
            "🔎 Мои поиски",
            "⚙️ Аккаунт",
            "👤 Аккаунт",
            "Аккаунт",
        ]
    ),
)
async def wizard_cancel_from_url(message: types.Message, state: FSMContext):
    await state.clear()
    await show_main_menu(message, clear_reply_keyboard=True)


@wizard_router.message(SearchWizard.url)
async def wizard_got_url(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    url_in = _extract_avito_url_text(raw) or raw
    try:
        parsed = parse_avito_url(url_in)
    except ValueError:
        await message.answer(
            _br("Похоже, это не ссылка Авито. Вставьте корректный URL.")
        )
        return
    if parsed.kind == "item":
        await message.answer(_br("Это ссылка на карточку объявления. Пришлите ссылку на результаты поиска Avito."))
        return
    url = parsed.canonical_url
    guessed = parsed.filters

    await state.update_data(
        url=url,
        display_url=parsed.display_url,
        warnings=list(parsed.warnings),
        guessed_kw=guessed.keywords_all,
        keywords_all=guessed.keywords_all,
        keywords_any=guessed.keywords_any,
        keywords_stop=guessed.keywords_stop,
        price_min=guessed.price_min,
        price_max=guessed.price_max,
    )
    await state.set_state(SearchWizard.confirm)
    await message.answer(
        _filter_preview(parsed),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _inline_button(
                        text="Подтвердить",
                        callback_data="wizard_filters_confirm",
                        style="success",
                    )
                ],
                [
                    _inline_button(
                        text="Изменить цены",
                        callback_data="wizard_filters_prices",
                        style="primary",
                    ),
                    _inline_button(
                        text="Изменить слова",
                        callback_data="wizard_filters_words",
                        style="primary",
                    ),
                ],
                [
                    _inline_button(
                        text="Отмена",
                        callback_data="wizard_filters_cancel",
                        style="danger",
                    )
                ],
            ],
        ),
        disable_web_page_preview=True,
    )


@wizard_router.callback_query(SearchWizard.confirm, F.data == "wizard_filters_confirm")
async def wizard_confirm_filters(cq: types.CallbackQuery, state: FSMContext):
    if isinstance(cq.message, types.Message):
        await _continue_after_filter_review(cq.message, state)
    await cq.answer()


@wizard_router.callback_query(SearchWizard.confirm, F.data == "wizard_filters_prices")
async def wizard_edit_prices(cq: types.CallbackQuery, state: FSMContext):
    await state.set_state(SearchWizard.price_min)
    if isinstance(cq.message, types.Message):
        await cq.message.answer("Введите минимальную цену или «-».")
    await cq.answer()


@wizard_router.callback_query(SearchWizard.confirm, F.data == "wizard_filters_words")
async def wizard_edit_words(cq: types.CallbackQuery, state: FSMContext):
    await state.set_state(SearchWizard.words)
    if isinstance(cq.message, types.Message):
        await cq.message.answer(
            "Введите списки в формате:\n"
            "<code>all=Samsung,Galaxy; any=256GB,512GB; stop=копия,ремонт</code>\n"
            "Для очистки всех слов отправьте «-»."
        )
    await cq.answer()


@wizard_router.callback_query(SearchWizard.confirm, F.data == "wizard_filters_cancel")
async def wizard_cancel_preview(cq: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if isinstance(cq.message, types.Message):
        await show_main_menu(cq.message, clear_reply_keyboard=True)
    await cq.answer()


def _parse_word_groups(text: str) -> dict[str, list[str]]:
    if text.strip() == "-":
        return {"all": [], "any": [], "stop": []}
    result: dict[str, list[str]] = {}
    for group in text.split(";"):
        if "=" not in group:
            continue
        key, raw_values = group.split("=", 1)
        key = key.strip().casefold()
        if key not in {"all", "any", "stop"}:
            continue
        result[key] = [value.strip() for value in raw_values.split(",") if value.strip()]
    return result


def _parse_add_options(
    text: str, base: SubscriberFilter
) -> tuple[SubscriberFilter, list[str]]:
    filters = normalize_filter(base)
    warnings: list[str] = []
    if not text.strip():
        return filters, warnings
    tokens = re.split(
        r"\s*,\s*(?=(?:kw|all|any|stop|min|max)\s*=)",
        text.strip(),
        flags=re.I,
    )
    for token in tokens:
        if "=" not in token:
            warnings.append(f"Неизвестная опция: {token.strip()}")
            continue
        key, value = token.split("=", 1)
        key = key.strip().casefold()
        value = value.strip()
        if key in {"kw", "all", "any", "stop"}:
            words = [word.strip() for word in re.split(r"[|;]", value) if word.strip()]
            attr = {
                "kw": "keywords_all",
                "all": "keywords_all",
                "any": "keywords_any",
                "stop": "keywords_stop",
            }[key]
            setattr(filters, attr, words)
        elif key in {"min", "max"}:
            parsed = None if value == "-" else _parse_price_input(value)
            if parsed is None and value != "-":
                warnings.append(f"Некорректное значение {key}: {value}")
                continue
            setattr(filters, "price_min" if key == "min" else "price_max", parsed)
        else:
            warnings.append(f"Неизвестная опция: {key}")
    if (
        filters.price_min is not None
        and filters.price_max is not None
        and filters.price_min > filters.price_max
    ):
        warnings.append("Минимальная цена не может быть больше максимальной")
    return normalize_filter(filters), warnings


@wizard_router.message(SearchWizard.words, F.text.casefold() == "отмена")
async def wizard_cancel_words(message: types.Message, state: FSMContext):
    await state.clear()
    await show_main_menu(message, clear_reply_keyboard=True)


@wizard_router.message(SearchWizard.words)
async def wizard_got_words(message: types.Message, state: FSMContext):
    groups = _parse_word_groups(message.text or "")
    if not groups:
        await message.answer("Не удалось разобрать списки. Используйте all=, any= и stop=.")
        return
    data = await state.get_data()
    await state.update_data(
        keywords_all=groups.get("all", data.get("keywords_all") or []),
        keywords_any=groups.get("any", data.get("keywords_any") or []),
        keywords_stop=groups.get("stop", data.get("keywords_stop") or []),
    )
    await _continue_after_filter_review(message, state)


@wizard_router.message(SearchWizard.price_min, F.text.casefold() == "отмена")
async def wizard_cancel_min(message: types.Message, state: FSMContext):
    await state.clear()
    await show_main_menu(message, clear_reply_keyboard=True)


@wizard_router.message(SearchWizard.price_min)
async def wizard_got_min(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    pmin = None
    if txt != "-":
        pmin = _parse_price_input(txt)
        if pmin is None:
            await message.answer(_br("Введите число или «-»."))
            return
    await state.update_data(price_min=pmin)
    await state.set_state(SearchWizard.price_max)
    await message.answer(
        "<b>Максимальная цена</b>\nВведите число или «-», если ограничения нет.",
        reply_markup=types.ReplyKeyboardMarkup(
            keyboard=[
                [
                    _keyboard_button(text="-", style="primary"),
                    _keyboard_button(text="Отмена", style="danger"),
                ],
            ],
            resize_keyboard=True,
        ),
    )


@wizard_router.message(SearchWizard.price_max, F.text.casefold() == "отмена")
async def wizard_cancel_max(message: types.Message, state: FSMContext):
    await state.clear()
    await show_main_menu(message, clear_reply_keyboard=True)


@wizard_router.message(SearchWizard.price_max)
async def wizard_got_max(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    pmax = None if txt == "-" else _parse_price_input(txt)
    if pmax is None and txt != "-":
        await message.answer(
            _br("Введите число или «-», если ограничения нет.")
        )
        return

    data = await state.get_data()
    url = data.get("url") or ""
    pmin = data.get("price_min")
    filters = _filter_from_data(data)
    if pmin is not None and pmax is not None and pmin > pmax:
        await message.answer("Минимальная цена не может быть больше максимальной.")
        return

    await _prompt_search_name(message, state, url, pmin, pmax, filters.keywords_all)


@wizard_router.message(SearchWizard.name, F.text.casefold() == "отмена")
async def wizard_cancel_name(message: types.Message, state: FSMContext):
    await state.clear()
    await show_main_menu(message, clear_reply_keyboard=True)


@wizard_router.message(SearchWizard.name)
async def wizard_finish_create(message: types.Message, state: FSMContext):
    lic: LicenseManager = cast(Any, message.bot).app.license
    if not lic.is_active(message.chat.id):
        await message.answer(
            _br(
                "Слот не активирован. Получите ключ у поддержки и отправьте его боту (формат «Ключ: xxxxx-…»)."
            )
        )
        await state.clear()
        return

    data = await state.get_data()
    url = data.get("url") or ""
    filters = _filter_from_data(data)
    name = (message.text or "").strip() or None

    parsed_spec = parse_avito_url(url)
    spec = SearchSpec(
        canonical_url=parsed_spec.canonical_url,
        display_url=str(data.get("display_url") or parsed_spec.display_url),
        search_key=parsed_spec.search_key,
        kind=parsed_spec.kind,
        filters=parsed_spec.filters,
        warnings=tuple(data.get("warnings") or parsed_spec.warnings),
    )
    sub = await cast(Any, message.bot).app.manager.add_search_spec(
        message.chat.id, spec, filters
    )
    sub.name = name
    cast(Any, message.bot).app.manager.save()

    await state.clear()
    title = html.escape(name or f"Поиск №{sub.id}")
    watcher = cast(Any, message.bot).app.manager.watchers.get(sub.search_key)
    await show_main_menu(
        message,
        f"✅ <b>{title}</b> создан\n"
        f"Конвертация API: <b>{_conversion_text(watcher)}</b>\n"
        "Новые объявления будут приходить в бот-оповещатель.",
        clear_reply_keyboard=True,
    )


# ===== Поддержка =====
support_router = Router(name="support")


@support_router.message(F.text.in_(["🛟 Поддержка", "Поддержка"]))
async def support_info(message: types.Message, state: FSMContext):
    await state.clear()
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [_inline_button(text="Написать в поддержку", url=SUPPORT_LINK)],
            [
                _inline_button(
                    text="Назад в меню", callback_data="main_menu"
                )
            ],
        ]
    )
    await message.answer(
        "<b>Поддержка</b>\n\nНапишите нам, если нужна помощь с доступом или настройкой поиска.",
        reply_markup=kb,
        disable_web_page_preview=True,
    )


@support_router.callback_query(F.data == "support")
async def support_callback(cq: types.CallbackQuery):
    if isinstance(cq.message, types.Message):
        kb = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _inline_button(
                        text="Написать в поддержку", url=SUPPORT_LINK
                    )
                ],
                [
                    _inline_button(
                        text="Назад в меню", callback_data="main_menu"
                    )
                ],
            ]
        )
        await cq.message.edit_text(
            "<b>Поддержка</b>\n\nПоможем с доступом, привязкой оповещений и настройкой поиска.",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    await cq.answer()


@support_router.callback_query(F.data == "help")
async def help_callback(cq: types.CallbackQuery):
    app = getattr(cq.bot, "app", None) or cast(Any, cq.bot).app
    text, kb = build_help_content(app, cq.from_user.id)
    if isinstance(cq.message, types.Message):
        await cq.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    await cq.answer()


@support_router.callback_query(F.data == "main_menu")
async def main_menu_callback(cq: types.CallbackQuery):
    if isinstance(cq.message, types.Message):
        await cq.message.edit_text(MAIN_MENU_TEXT, reply_markup=MAIN_INLINE_KB)
    await cq.answer()


# ===== Экран «Поиски» =====
def build_searches_kb(
    subs: List[Subscription],
    lic: LicenseManager,
    user_id: int,
    watchers: Optional[Dict[str, "Watcher"]] = None,
) -> types.InlineKeyboardMarkup:
    rows: List[List[types.InlineKeyboardButton]] = []
    for s in subs:
        label = s.name or f"Поиск №{s.id}"
        status = get_watcher_status(s.search_key, watchers or {})
        rows.append(
            [
                _inline_button(
                    text=f"{status}  {label}",
                    callback_data=f"open_sub:{s.id}",
                )
            ]
        )
    if lic.is_active(user_id):
        rows.append(
            [
                _inline_button(
                    text="＋ Создать поиск", callback_data="slot_new", style="success"
                )
            ]
        )
    else:
        rows.append(
            [
                _inline_button(
                    text="🔑 Получить доступ", callback_data="get_slot", style="success"
                )
            ]
        )
    rows.append(
        [_inline_button(text="Назад в меню", callback_data="main_menu")]
    )
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _short_wait(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} сек"
    minutes = max(1, (seconds + 59) // 60)
    if minutes < 60:
        return f"{minutes} мин"
    return f"{max(1, (minutes + 59) // 60)} ч"


def get_watcher_status(
    search_key: str, watchers: Optional[Dict[str, "Watcher"]]
) -> str:
    w = (watchers or {}).get(search_key)
    if w is None:
        return "⚪"
    if w.task is None or w.task.done():
        return "⚪"
    if w.conversion_status == "pending":
        return "🟡 ожидает"
    if w.conversion_status == "retry":
        return "🟡 повтор"

    now = time.monotonic()
    route_until = Watcher._route_blocked_until.get(w._route_key(), 0.0)
    wait = int(max(w._blocked_until, route_until) - now)
    if wait > 0:
        return f"🟡 {_short_wait(wait)}"
    if w.last_http_status and w.last_http_status != 200:
        return "🟡"
    return "🟢"


def format_sub_panel(
    sub: Subscription,
    lic: LicenseManager,
    watcher: Optional[Watcher] = None,
) -> str:
    title = html.escape(sub.name or f"Поиск №{sub.id}")
    exp = lic.expiry_dt(sub.user_id)
    price_min = (
        f"{sub.flt.price_min:,}".replace(",", " ") + " ₽"
        if sub.flt.price_min is not None
        else "без минимума"
    )
    price_max = (
        f"{sub.flt.price_max:,}".replace(",", " ") + " ₽"
        if sub.flt.price_max is not None
        else "не задана"
    )
    target = (
        ", ".join(html.escape(word) for word in sub.flt.keywords_all) or "не заданы"
    )
    any_words = (
        ", ".join(html.escape(word) for word in sub.flt.keywords_any) or "не заданы"
    )
    stop = ", ".join(html.escape(word) for word in sub.flt.keywords_stop) or "не заданы"
    sort_title = html.escape(
        str(extract_url_metadata(sub.url).get("sort_title") or "По умолчанию")
    )
    url = html.escape(avito_short_url(sub.url), quote=True)
    access = exp.strftime("%d.%m.%Y %H:%M") if exp else "не активен"
    return (
        f"🔎 <b>{title}</b>\n"
        f"⏳ Доступ до: <b>{access}</b>\n\n"
        f"⚙️ <b>Фильтры</b>\n"
        f"💰 Цена: <b>{price_min} — {price_max}</b>\n"
        f"🔤 Обязательные слова: {target}\n"
        f"🔀 Хотя бы одно: {any_words}\n"
        f"🚫 Исключить: {stop}\n"
        f"↕️ Сортировка Avito: <b>{sort_title}</b>\n"
        f"🆕 Только новые после запуска: "
        f"<b>{'включено' if sub.only_new else 'выключено'}</b>\n\n"
        f"📡 API: <b>{_conversion_text(watcher)}</b>\n\n"
        f'🔗 <a href="{url}">Открыть поиск на Avito</a>'
    )


def build_sub_inline_kb(sub: Subscription) -> types.InlineKeyboardMarkup:
    rid = sub.id
    rows = [
        [
            _inline_button(text="Цена от", callback_data=f"sub:{rid}:min"),
            _inline_button(text="Цена до", callback_data=f"sub:{rid}:max"),
        ],
        [
            _inline_button(
                text="Обязательные", callback_data=f"sub:{rid}:pos"
            ),
            _inline_button(text="Хотя бы одно", callback_data=f"sub:{rid}:any"),
        ],
        [
            _inline_button(
                text="Исключить", callback_data=f"sub:{rid}:stop"
            ),
            _inline_button(
                text=f"Только новые: {'вкл' if sub.only_new else 'выкл'}",
                callback_data=f"sub:{rid}:toggle_new",
                style="success" if sub.only_new else "danger",
            ),
        ],
        [
            _inline_button(
                text="Обновить сейчас", callback_data=f"force_update:{sub.id}"
            )
        ],
        [
            _inline_button(text="Назад", callback_data="back_to_list"),
            _inline_button(
                text="Удалить", callback_data=f"sub:{rid}:delete", style="danger"
            ),
        ],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


class EditPrice(StatesGroup):
    value = State()


class EditWords(StatesGroup):
    mode = State()
    sub_id = State()
    text = State()


searches_router = Router(name="searches")


def _searches_view(app: Any, user_id: int) -> tuple[str, types.InlineKeyboardMarkup]:
    subs = app.manager.list_user_subs(user_id)
    kb = build_searches_kb(subs, app.license, user_id, watchers=app.manager.watchers)
    if subs:
        text = "<b>Мои поиски</b>\nВыберите поиск для настройки."
    elif app.license.is_active(user_id):
        text = "<b>Мои поиски</b>\nУ вас пока нет настроенных поисков."
    else:
        text = "<b>Мои поиски</b>\nДля создания поиска нужен активный доступ."
    return text, kb


async def _send_searches_screen(message: types.Message, user_id: int) -> None:
    app = cast(Any, message.bot).app
    text, kb = _searches_view(app, user_id)
    await message.answer(text, reply_markup=kb)


async def _edit_searches_screen(message: types.Message, user_id: int) -> None:
    app = cast(Any, message.bot).app
    text, kb = _searches_view(app, user_id)
    await message.edit_text(text, reply_markup=kb)


@searches_router.message(F.text.in_(["🧭 Поиски", "🔎 Мои поиски", "Поиски"]))
async def searches_screen(message: types.Message, state: FSMContext):
    await state.clear()
    await _send_searches_screen(message, message.chat.id)


@searches_router.callback_query(F.data == "searches")
async def cb_menu_searches(cq: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if isinstance(cq.message, types.Message):
        await _edit_searches_screen(cq.message, cq.from_user.id)
    await cq.answer()


@searches_router.callback_query(F.data.startswith("force_update:"))
async def cb_force_update(cq: types.CallbackQuery):
    data_str: str = cq.data or ""
    try:
        sub_id = int(data_str.split(":", 1)[1])
    except (IndexError, ValueError):
        await cq.answer()
        return

    app = cast(Any, cq.bot).app
    sub = app.manager.get_sub_by_id(cq.from_user.id, sub_id)
    if not sub:
        await cq.answer("Поиск не найден", show_alert=True)
        return

    watcher = app.manager.watchers.get(sub.search_key)
    if not watcher:
        await cq.answer("Вотчер не запущен", show_alert=True)
        return

    try:
        await watcher.stop()
        await watcher.start()
    except Exception as exc:
        logger.warning("force_update failed for %s: %s", sub.search_key, exc)
        await cq.answer("Не удалось перезапустить", show_alert=True)
        return

    await cq.answer("Проверка запущена")


@searches_router.callback_query(F.data == "get_slot")
async def cb_get_slot(cq: types.CallbackQuery):
    if isinstance(cq.message, types.Message):
        kb = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _inline_button(
                        text="Написать в поддержку", url=SUPPORT_LINK
                    )
                ],
                [
                    _inline_button(
                        text="Назад к поискам", callback_data="back_to_list"
                    )
                ],
            ]
        )
        await cq.message.edit_text(
            "<b>Доступ к мониторингу</b>\n\n"
            "Получите ключ у поддержки, затем отправьте его в этот чат.",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    await cq.answer()


@searches_router.callback_query(F.data == "slot_new")
async def cb_slot_new(cq: types.CallbackQuery, state: FSMContext):
    if not isinstance(cq.message, types.Message):
        await cq.answer()
        return
    await state.clear()
    await state.set_state(SearchWizard.url)
    await cq.message.answer(
        "<b>Новый поиск · 1 из 3</b>\n\n"
        "Пришлите ссылку на результаты поиска Avito. Все нужные категории и параметры выберите на сайте заранее.",
        reply_markup=types.ReplyKeyboardRemove(),
        disable_web_page_preview=True,
    )
    await cq.answer()


@searches_router.callback_query(F.data == "close_menu")
async def cb_close_menu(cq: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if isinstance(cq.message, types.Message):
        try:
            await cq.message.delete()
        except Exception:
            try:
                await cq.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
    await cq.answer("Закрыто")


@searches_router.callback_query(F.data == "back_to_list")
async def cb_back_to_list(cq: types.CallbackQuery):
    if isinstance(cq.message, types.Message):
        await _edit_searches_screen(cq.message, cq.from_user.id)
    await cq.answer()


@searches_router.callback_query(F.data.startswith("open_sub:"))
async def cb_open_sub(cq: types.CallbackQuery, state: FSMContext):
    data_str: str = cq.data or ""
    try:
        sub_id = int(data_str.split(":", 1)[1])
    except Exception:
        await cq.answer()
        return
    mng = cast(Any, cq.bot).app.manager
    sub = mng.get_sub_by_id(cq.from_user.id, sub_id)
    if not sub:
        await cq.answer("Подписка не найдена", show_alert=True)
        return
    if isinstance(cq.message, types.Message):
        lic: LicenseManager = cast(Any, cq.bot).app.license
        await cq.message.edit_text(
            format_sub_panel(sub, lic, mng.watchers.get(sub.search_key)),
            reply_markup=build_sub_inline_kb(sub),
            disable_web_page_preview=True,
        )
    await cq.answer()


@searches_router.callback_query(F.data.startswith("sub:"))
async def cb_sub_actions(cq: types.CallbackQuery, state: FSMContext):
    data_str: str = cq.data or ""
    m = re.match(r"sub:(\d+):(\w+)", data_str)
    if not m:
        await cq.answer()
        return
    sub_id = int(m.group(1))
    action = m.group(2)
    mng = cast(Any, cq.bot).app.manager
    sub = mng.get_sub_by_id(cq.from_user.id, sub_id)
    if not sub:
        await cq.answer("Подписка не найдена", show_alert=True)
        return

    if action in ("min", "max"):
        await state.set_state(EditPrice.value)
        await state.update_data(field=action, sub_id=sub.id)
        if isinstance(cq.message, types.Message):
            label = "минимальную" if action == "min" else "максимальную"
            await cq.message.answer(
                f"Введите новую {label} цену числом или «-», чтобы убрать ограничение."
            )
        await cq.answer()
        return

    if action in ("pos", "any", "stop"):
        await state.set_state(EditWords.text)
        await state.update_data(mode=action, sub_id=sub.id)
        if isinstance(cq.message, types.Message):
            hint = {
                "pos": "обязательные слова (должны встретиться все)",
                "any": "слова-варианты (достаточно хотя бы одного)",
                "stop": "слова для исключения (минус-слова)",
            }[action]
            current_words = {
                "pos": sub.flt.keywords_all,
                "any": sub.flt.keywords_any,
                "stop": sub.flt.keywords_stop,
            }[action]
            curr = (
                ", ".join(current_words)
                or "—"
            )
            await cq.message.answer(
                f"Введите {hint} через запятую. Чтобы очистить список, отправьте «-».\n"
                f"Сейчас: <code>{html.escape(curr)}</code>"
            )
        await cq.answer()
        return

    if action == "toggle_new":
        sub.only_new = not sub.only_new
        mng.save()
        if isinstance(cq.message, types.Message):
            lic: LicenseManager = cast(Any, cq.bot).app.license
            await cq.message.edit_text(
                format_sub_panel(sub, lic, mng.watchers.get(sub.search_key)),
                reply_markup=build_sub_inline_kb(sub),
                disable_web_page_preview=True,
            )
        await cq.answer("Обновлено")
        return

    if action == "delete":
        if isinstance(cq.message, types.Message):
            title = html.escape(sub.name or f"Поиск №{sub.id}")
            kb = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        _inline_button(
                            text="Удалить поиск",
                            callback_data=f"sub:{sub.id}:delete_confirm",
                            style="danger",
                        )
                    ],
                    [
                        _inline_button(
                            text="Отмена", callback_data=f"sub:{sub.id}:delete_cancel"
                        )
                    ],
                ]
            )
            await cq.message.edit_text(
                f"<b>Удалить «{title}»?</b>\nЭто действие нельзя отменить.",
                reply_markup=kb,
            )
        await cq.answer()
        return

    if action == "delete_cancel":
        if isinstance(cq.message, types.Message):
            lic: LicenseManager = cast(Any, cq.bot).app.license
            await cq.message.edit_text(
                format_sub_panel(sub, lic, mng.watchers.get(sub.search_key)),
                reply_markup=build_sub_inline_kb(sub),
                disable_web_page_preview=True,
            )
        await cq.answer()
        return

    if action == "delete_confirm":
        ok = await mng.remove_subscription(cq.from_user.id, sub.id)
        if isinstance(cq.message, types.Message):
            text, kb = _searches_view(cast(Any, cq.bot).app, cq.from_user.id)
            notice = "Поиск удалён.\n\n" if ok else "Не удалось удалить поиск.\n\n"
            await cq.message.edit_text(notice + text, reply_markup=kb)
        await cq.answer()
        return

    await cq.answer()


# ====== Применение изменений из FSM ======
@searches_router.message(EditPrice.value, F.text.casefold() == "отмена")
@searches_router.message(EditWords.text, F.text.casefold() == "отмена")
async def ui_edit_cancel(m: types.Message, state: FSMContext):
    await state.clear()
    await _send_searches_screen(m, m.chat.id)


@searches_router.message(EditPrice.value)
async def ui_edit_apply_max(m: types.Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("field")
    raw = data.get("sub_id")
    if raw is None:
        await state.clear()
        await m.reply(
            _br("Ошибка: нет идентификатора поиска."), reply_markup=MAIN_INLINE_KB
        )
        return
    sub_id = int(raw)

    txt = (m.text or "").strip()
    mng = cast(Any, m.bot).app.manager
    sub = mng.get_sub_by_id(m.chat.id, sub_id)
    if not sub:
        await state.clear()
        await m.reply(_br("Подписка не найдена."), reply_markup=MAIN_INLINE_KB)
        return
    if field in ("min", "max"):
        parsed = None if txt == "-" else _parse_price_input(txt)
        if parsed is None and txt != "-":
            label = "Минимальная" if field == "min" else "Максимальная"
            await m.reply(
                _br(f"{label} цена должна быть числом или «-».")
            )
            return
        if field == "min":
            if parsed is not None and sub.flt.price_max is not None and parsed > sub.flt.price_max:
                await m.reply("Минимальная цена не может быть больше максимальной.")
                return
            sub.flt.price_min = parsed
        else:
            if parsed is not None and sub.flt.price_min is not None and parsed < sub.flt.price_min:
                await m.reply("Максимальная цена не может быть меньше минимальной.")
                return
            sub.flt.price_max = parsed
        mng.save()
    await state.clear()
    lic: LicenseManager = cast(Any, m.bot).app.license
    await m.reply(
        format_sub_panel(sub, lic, mng.watchers.get(sub.search_key)),
        reply_markup=build_sub_inline_kb(sub),
        disable_web_page_preview=True,
    )


@searches_router.message(EditWords.text)
async def ui_edit_words_apply(m: types.Message, state: FSMContext):
    data = await state.get_data()
    mode = data.get("mode") or "pos"
    raw = data.get("sub_id")
    if raw is None:
        await state.clear()
        await m.reply(
            _br("Ошибка: нет идентификатора поиска."), reply_markup=MAIN_INLINE_KB
        )
        return
    sub_id = int(raw)

    mng = cast(Any, m.bot).app.manager
    sub = mng.get_sub_by_id(m.chat.id, sub_id)
    if not sub:
        await state.clear()
        await m.reply(_br("Подписка не найдена."), reply_markup=MAIN_INLINE_KB)
        return
    raw_words = (m.text or "").strip()
    words = (
        []
        if raw_words == "-"
        else [w.strip() for w in raw_words.replace(";", ",").split(",") if w.strip()]
    )
    if mode == "pos":
        sub.flt.keywords_all = words
    elif mode == "any":
        sub.flt.keywords_any = words
    else:
        sub.flt.keywords_stop = words
    sub.flt = normalize_filter(sub.flt)
    mng.save()
    await state.clear()
    lic: LicenseManager = cast(Any, m.bot).app.license
    await m.reply(
        format_sub_panel(sub, lic, mng.watchers.get(sub.search_key)),
        reply_markup=build_sub_inline_kb(sub),
        disable_web_page_preview=True,
    )


# ===== Приём ключа (устойчивый) =====
key_router = Router(name="keys")


async def _accept_key_impl(m: types.Message, state: FSMContext):
    app = cast(Any, m.bot).app
    lic: LicenseManager = app.license

    txt = (m.text or "").strip()
    m_uuid = KEY_RE.search(txt)  # <-- ИЩЕМ ВЕЗДЕ, не только в начале
    if not m_uuid:
        return

    key_value = m_uuid.group(1)
    try:
        uuid.UUID(key_value)
    except Exception:
        await m.reply(_br("Ключ недействителен."))
        return

    res = app.redeem_key(key_value)
    if not res:
        await state.clear()
        await m.reply(_br("Ключ недействителен или уже использован."))
        return

    hours, expires_ts = res  # expires_ts формируется в момент активации!
    logger.info(f"User {m.chat.id} activated key: {key_value}")
    await state.clear()
    lic.activate_until(m.chat.id, expires_ts)
    exp_str = _fmt_dt(expires_ts)

    # учёт аккаунта
    app.account_register_if_needed(m.chat.id)
    app.account_add_key(m.chat.id, key_value, hours, expires_ts)

    # Кнопка для привязки alert-бота
    deeplink = app.alert_deeplink(m.chat.id)
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _inline_button(
                    text="1. Подключить оповещения",
                    url=deeplink,
                    style="success",
                )
            ],
            [
                _inline_button(
                    text="2. Создать поиск", callback_data="slot_new"
                )
            ],
        ]
    )

    await m.reply(
        f"✅ <b>Доступ активирован</b>\nДействует до: <b>{exp_str}</b>\n\n"
        "Сначала подключите оповещения, затем создайте поиск.",
        disable_web_page_preview=True,
        reply_markup=kb,
    )


# Матчим как «Ключ: …», так и просто UUID
@key_router.message(
    F.text.regexp(
        r"(?i)(?:^|\s)(?:ключ\s*:\s*)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:\s|$)"
    )
)
async def accept_key_regexp(m: types.Message, state: FSMContext):
    await _accept_key_impl(m, state)


# ===== Аккаунт =====
account_router = Router(name="account")


def account_panel_text(app: Any, user_id: int) -> str:
    lic: LicenseManager = app.license
    acc = app.account_get(user_id)
    subs = app.manager.list_user_subs(user_id)
    exp_dt = lic.expiry_dt(user_id)

    key_txt = "—"
    if acc and acc.get("keys"):
        now = time.time()
        current = None
        for k in sorted(acc["keys"], key=lambda x: x.get("activated", 0), reverse=True):
            if float(k.get("expires", 0) or 0) > now:
                current = k
                break
        if not current:
            current = sorted(
                acc["keys"], key=lambda x: x.get("activated", 0), reverse=True
            )[0]
        key_value = str(current.get("key", ""))
        key_txt = f"•••• {key_value[-4:]}" if key_value else "—"

    lines = [
        "👤 <b>Аккаунт</b>",
        f"ID: <code>{user_id}</code>",
        f"Регистрация: {_fmt_dt((acc or {}).get('registered')) if acc else '—'}",
        "",
        f"Ключ: <code>{key_txt}</code>",
        f"Доступ до: <b>{exp_dt.strftime('%d.%m.%Y %H:%M') if exp_dt else 'не активен'}</b>",
        f"Поисков: <b>{len(subs)}</b>",
    ]
    return "\n".join(lines)


def build_account_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _inline_button(
                    text="История ключей", callback_data="account:expired"
                ),
                _inline_button(
                    text="Продлить доступ", url=SUPPORT_LINK, style="success"
                ),
            ],
            [
                _inline_button(
                    text="Назад в меню", callback_data="main_menu"
                )
            ],
        ]
    )


@account_router.message(F.text.in_(["⚙️ Аккаунт", "👤 Аккаунт", "Аккаунт", "/account"]))
async def account_show(m: types.Message, state: FSMContext):
    app = cast(Any, m.bot).app
    app.account_register_if_needed(m.chat.id)
    txt = account_panel_text(app, m.chat.id)
    await m.answer(txt, reply_markup=build_account_kb())


@account_router.callback_query(F.data == "account")
async def account_callback(cq: types.CallbackQuery):
    app = cast(Any, cq.bot).app
    app.account_register_if_needed(cq.from_user.id)
    if isinstance(cq.message, types.Message):
        await cq.message.edit_text(
            account_panel_text(app, cq.from_user.id),
            reply_markup=build_account_kb(),
        )
    await cq.answer()


@account_router.callback_query(F.data == "account:expired")
async def account_expired(cq: types.CallbackQuery):
    app = cast(Any, cq.bot).app
    acc = app.account_get(cq.from_user.id) or {}
    items = []
    now = time.time()
    for k in acc.get("keys") or []:
        exp_ts = float(k.get("expires") or 0)
        if exp_ts and exp_ts < now:
            items.append(f"• <code>{k.get('key', '')}</code> — истёк {_fmt_dt(exp_ts)}")
    text = (
        "Истёкших ключей не найдено."
        if not items
        else "<b>Истёкшие ключи:</b>\\n" + "\\n".join(items)
    )
    if isinstance(cq.message, types.Message):
        await cq.message.answer(_br(text))
    await cq.answer()


# ===== Главное приложение =====
class App:
    def __init__(self, token: str):
        self.bot = Bot(
            token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
        )
        cast(Any, self.bot).app = self
        self.dp = Dispatcher(storage=MemoryStorage())
        self.manager = WatcherManager(self.bot)
        self.license = LicenseManager()
        self.accounts = AccountService(
            self.license,
            accounts_file=ACCOUNTS_FILE,
            keys_file=KEYS_FILE,
        )

        # alert
        self.alert_token: Optional[str] = ALERT_BOT_TOKEN
        self.alert_username: str = ALERT_BOT_USERNAME
        self.alert_message_effect_id: Optional[str] = ALERT_MESSAGE_EFFECT_ID
        self.bindings_file: str = BINDINGS_FILE
        self.alert_links_file: str = ALERT_LINKS_FILE
        self._alert_warned: Set[int] = set()
        self._alert_session: Optional[aiohttp.ClientSession] = None

        # storage
        self.keys_file: str = KEYS_FILE
        self.sent_file: str = SENT_FILE
        self.accounts_file: str = ACCOUNTS_FILE

        self.accounts._restore_licenses()
        self._register()

    # ===== работа с привязками main_user_id -> alert_chat_id
    def _load_bindings(self) -> Dict[str, int]:
        try:
            data = load_state(self.bindings_file, {}) or {}
            return {str(k): int(v) for k, v in data.items()}
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Не удалось прочитать привязки: %s", exc)
            return {}

    def _save_bindings(self, data: Dict[str, int]) -> None:
        save_state(self.bindings_file, data)

    def get_alert_chat_id(self, main_user_id: int) -> Optional[int]:
        b = self._load_bindings()
        val = b.get(str(main_user_id))
        return int(val) if val else None

    def alert_deeplink(self, main_user_id: int) -> str:
        uname = self.alert_username or "<УКАЖИТЕ_ALERT_BOT_USERNAME>"
        token = secrets.token_urlsafe(24)
        now = time.time()

        def add_link(links):
            expired = [k for k, v in links.items() if float(v.get("expires", 0)) < now]
            for key in expired:
                del links[key]
            links[token] = {"user_id": main_user_id, "expires": now + 600}

        update_state(self.alert_links_file, {}, add_link)
        return f"https://t.me/{uname}?start={token}"

    def missing_alert_hint_once(self, user_id: int) -> bool:
        if user_id in self._alert_warned:
            return False
        self._alert_warned.add(user_id)
        return True

    async def send_to_alert(
        self, alert_chat_id: int, caption: str, image_url: Optional[str]
    ) -> bool:
        """Send a notification through the alert bot's reusable HTTP session."""
        if not self.alert_token:
            return False
        api = f"https://api.telegram.org/bot{self.alert_token}"
        safe_caption = _br(caption)
        reply_markup = _alert_reply_markup(safe_caption)
        if self._alert_session is None or self._alert_session.closed:
            self._alert_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        session = self._alert_session
        try:
            effect_attempts = (
                (self.alert_message_effect_id, None)
                if self.alert_message_effect_id
                else (None,)
            )
            for effect_id in effect_attempts:
                if image_url:
                    form = aiohttp.FormData()
                    form.add_field("chat_id", str(alert_chat_id))
                    form.add_field("caption", safe_caption)
                    form.add_field("parse_mode", "HTML")
                    form.add_field("photo", image_url)
                    if reply_markup:
                        form.add_field(
                            "reply_markup",
                            json.dumps(reply_markup, ensure_ascii=False),
                        )
                    if effect_id:
                        form.add_field("message_effect_id", effect_id)
                    request = session.post(f"{api}/sendPhoto", data=form)
                else:
                    payload = {
                        "chat_id": alert_chat_id,
                        "text": safe_caption,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    }
                    if reply_markup:
                        payload["reply_markup"] = reply_markup
                    if effect_id:
                        payload["message_effect_id"] = effect_id
                    request = session.post(f"{api}/sendMessage", json=payload)

                async with request as response:
                    body = await response.json(content_type=None)
                    if (
                        response.status == 200
                        and isinstance(body, dict)
                        and body.get("ok") is True
                    ):
                        return True
                    if effect_id:
                        logger.warning(
                            "Alert message effect was rejected; retrying without it: %s",
                            body,
                        )
                        continue
                    logger.error(
                        "Alert Bot API rejected notification: status=%s response=%s",
                        response.status,
                        body,
                    )
                    return False
            return False
        except (aiohttp.ClientError, asyncio.TimeoutError, TypeError, ValueError) as exc:
            logger.exception("Alert notification failed: %s", exc)
            return False

    async def close(self) -> None:
        await self.manager.close()
        if self._alert_session is not None and not self._alert_session.closed:
            await self._alert_session.close()
        await self.bot.session.close()

    # ===== ключи =====
    def _load_keys(self) -> Dict[str, Dict[str, Any]]:
        return self.accounts._load_keys()

    def _save_keys(self, data: Dict[str, Dict[str, Any]]) -> None:
        self.accounts._save_keys(data)

    def issue_key(self, hours: int = 24, uses: int = 1) -> str:
        return self.accounts.issue_key(hours, uses)

    def redeem_key(self, key: str) -> Optional[tuple[int, float]]:
        """
        Возвращает (hours, expires_ts). ВАЖНО: expires_ts формируется в МОМЕНТ АКТИВАЦИИ,
        то есть ровно «сейчас + hours*3600», независимо от времени выдачи ключа.
        """

        return self.accounts.redeem_key(key)

    # ===== антидубликаты per-user + global =====
    def _load_sent(self) -> Dict[str, Dict[str, float]]:
        try:
            data = load_state(self.sent_file, {}) or {}
            out: Dict[str, Dict[str, float]] = {}
            for uk, mp in data.items():
                if isinstance(mp, dict):
                    out[str(uk)] = {str(aid): float(ts) for aid, ts in mp.items()}
            return out
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Не удалось прочитать dedup: %s", exc)
            return {}

    def _save_sent(self, data: Dict[str, Dict[str, float]]) -> None:
        save_state(self.sent_file, data)

    def _cleanup_sent_locked(self, data: Dict[str, Dict[str, float]]) -> None:
        cutoff = time.time() - DEDUP_TTL_DAYS * 86400
        for uk in list(data.keys()):
            inner = data.get(uk, {})
            for ad_id in list(inner.keys()):
                if inner[ad_id] < cutoff:
                    del inner[ad_id]
            if not inner:
                del data[uk]

    def sent_was_delivered(self, user_id: int, ad_id: str) -> bool:
        data = self._load_sent()
        inner = data.get(str(user_id), {})
        return ad_id in inner

    def sent_mark(self, user_id: int, ad_id: str, ts: Optional[float] = None) -> None:
        def mark(data):
            data.setdefault(str(user_id), {})[ad_id] = float(ts or time.time())
            self._cleanup_sent_locked(data)

        update_state(self.sent_file, {}, mark)

    # ===== аккаунты =====
    def _load_accounts(self) -> Dict[str, Dict[str, Any]]:
        return self.accounts._load_accounts()

    def _save_accounts(self, data: Dict[str, Dict[str, Any]]) -> None:
        save_state(self.accounts_file, data)

    def _restore_licenses(self) -> None:
        self.accounts._restore_licenses()

    def account_register_if_needed(self, user_id: int) -> None:
        self.accounts.register_if_needed(user_id)

    def account_add_key(
        self, user_id: int, key_value: str, hours: int, exp_ts: Optional[float]
    ):
        self.accounts.add_key(user_id, key_value, hours, exp_ts)

    def account_get(self, user_id: int) -> Optional[Dict[str, Any]]:
        return self.accounts.get(user_id)

    def _register(self):
        # Регистрируем key_router ПЕРВЫМ — чтобы сообщения с ключами обрабатывались гарантированно
        self.dp.include_router(key_router)
        self.dp.include_router(wizard_router)
        self.dp.include_router(support_router)
        self.dp.include_router(searches_router)
        self.dp.include_router(account_router)

        @self.dp.message(Command("test_alert"))
        async def test_alert(m: types.Message):
            if ADMIN_CHAT_ID is None or m.chat.id != ADMIN_CHAT_ID:
                await m.reply(_br("Команда доступна только админу."))
                return
            chat_id = self.get_alert_chat_id(m.chat.id)
            if not chat_id:
                await m.reply(
                    _br(
                        "Alert binding не найден. Нажмите кнопку привязки после активации ключа."
                    )
                )
                return
            ok = await self.send_to_alert(
                chat_id, _br("Тестовое сообщение из основного бота ✅"), None
            )
            await m.reply(
                _br(
                    "Отправлено."
                    if ok
                    else "Не удалось отправить (проверь токен ALERT_BOT_TOKEN и привязку)."
                )
            )

        @self.dp.message(F.text == "Назад")
        async def back_btn(m: types.Message, state: FSMContext):
            await state.clear()
            await show_main_menu(m, clear_reply_keyboard=True)

        @self.dp.message(
            F.text.in_(
                ["📘 Инструкция", "Инструкция", "ℹ️ Помощь", "Помощь", "/help"]
            )
        )
        async def help_cmd(m: types.Message):
            self.account_register_if_needed(m.chat.id)
            text, kb = build_help_content(self, m.chat.id)
            await m.answer(
                text, reply_markup=kb, disable_web_page_preview=True
            )

        @self.dp.message(Command("start"))
        async def start_cmd(m: types.Message):
            self.account_register_if_needed(m.chat.id)
            await show_main_menu(m, clear_reply_keyboard=True)

        @self.dp.message(Command("add"))
        async def add_cmd(m: types.Message):
            self.account_register_if_needed(m.chat.id)
            lic: LicenseManager = self.license
            if not lic.is_active(m.chat.id):
                await m.reply(
                    _br(
                        "Слот не активирован. Получите ключ у поддержки и отправьте его боту (формат «Ключ: …»)."
                    )
                )
                return
            txt = m.text or ""
            url_in = _extract_avito_url_text(txt)
            if not url_in:
                await m.reply(
                    _br(
                        "Укажите ссылку после /add. Пример:\\n"
                        "/add https://www.avito.ru/... max=100000000,min=10000,any=256GB|512GB"
                    )
                )
                return
            url = url_in.strip().strip(".,;:!?)]}>'\"»")
            try:
                parsed_url = parse_avito_url(url)
            except ValueError:
                await m.reply(
                    _br("Разрешены только ссылки https://avito.ru и его поддоменов.")
                )
                return
            if parsed_url.kind == "item":
                await m.reply(_br("Это ссылка на карточку объявления. Нужна ссылка на результаты поиска Avito."))
                return
            url_end = txt.find(url_in) + len(url_in)
            params = txt[url_end:].strip().lstrip(",")
            flt, option_warnings = _parse_add_options(params, parsed_url.filters)
            if option_warnings:
                await m.reply(
                    "Не удалось применить параметры:\n"
                    + "\n".join(f"• {html.escape(value)}" for value in option_warnings)
                )
                return
            sub = await self.manager.add_search_spec(m.chat.id, parsed_url, flt)
            watcher = self.manager.watchers.get(sub.search_key)
            all_warnings = [*parsed_url.warnings, *option_warnings]
            warning_text = (
                "\\nПредупреждения: "
                + "; ".join(html.escape(value) for value in all_warnings)
                if all_warnings
                else ""
            )
            await m.reply(
                _br(
                    f"Подписка добавлена: <b>{sub.id}</b>\\n"
                    f"Ссылка: {parsed_url.canonical_url}\\n"
                    f"Конвертация API: <b>{_conversion_text(watcher)}</b>\\n"
                    "Карточки будут приходить в бот-оповещатель."
                    + warning_text
                ),
                disable_web_page_preview=True,
            )

        @self.dp.message(Command("list"))
        async def list_cmd(m: types.Message):
            self.account_register_if_needed(m.chat.id)
            subs = self.manager.list_user_subs(m.chat.id)
            if not subs:
                await m.reply(_br("У вас нет активных подписок."))
                return
            lines = ["Ваши подписки:"]
            for s in subs:
                desc = []
                if s.name:
                    desc.append(f"name={s.name}")
                if s.flt.keywords_all:
                    desc.append(f"kw={','.join(s.flt.keywords_all)}")
                if s.flt.keywords_stop:
                    desc.append(f"stop={','.join(s.flt.keywords_stop)}")
                if s.flt.price_min is not None:
                    desc.append(f"min={s.flt.price_min}")
                if s.flt.price_max is not None:
                    desc.append(f"max={s.flt.price_max}")
                lines.append(
                    f"{s.id}: {s.url} " + (f"({' ; '.join(desc)})" if desc else "")
                )
            lines.append(
                "\\nПодсказка: карточки приходят в бот-оповещатель после привязки."
            )
            await m.reply(_br("\\n".join(lines)), disable_web_page_preview=True)

        @self.dp.message(Command("remove"))
        async def remove_cmd(m: types.Message):
            self.account_register_if_needed(m.chat.id)
            parts = (m.text or "").split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip().isdigit():
                await m.reply(_br("Укажите ID: /remove 3"))
                return
            ok = await self.manager.remove_subscription(
                m.chat.id, int(parts[1].strip())
            )
            await m.reply(_br("Удалено." if ok else "Подписка не найдена."))

        @self.dp.message(Command("genkey"))
        async def genkey_cmd(m: types.Message):
            if ADMIN_CHAT_ID is None or m.chat.id != ADMIN_CHAT_ID:
                await m.reply(_br("Команда доступна только админу."))
                return
            parts = (m.text or "").split()
            hours = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 24
            uses = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 1
            key = self.issue_key(hours=hours, uses=uses)
            await m.reply(
                _br(
                    "Сгенерирован ключ ✅\\n"
                    f"Ключ: <code>{key}</code>\\n"
                    f"Срок: {hours} ч\\n"
                    f"Использований: {uses}\\n\\n"
                    "Отправьте пользователю в виде: <b>Ключ: </b><code>значение</code>"
                )
            )

        @self.dp.message(Command("diag"))
        async def diag_cmd(m: types.Message):
            if ADMIN_CHAT_ID is None or m.chat.id != ADMIN_CHAT_ID:
                await m.reply(_br("Команда доступна только админу."))
                return
            lic = "✅" if self.license.is_active(m.chat.id) else "❌"
            bind = self.get_alert_chat_id(m.chat.id)
            subs = self.manager.list_user_subs(m.chat.id)
            watchers = list(self.manager.watchers.items())
            lines = [
                "<b>Диагностика</b>",
                f"Лицензия: {lic}",
                f"Alert binding: {'✅ ' + str(bind) if bind else '❌'}",
                f"Подписок у этого юзера: {len(subs)}",
                f"Активных вотчеров: {len(watchers)}",
            ]
            if watchers:
                lines.append("Ключи вотчеров:")
                now_mono = time.monotonic()
                for key, watcher in watchers:
                    route_until = Watcher._route_blocked_until.get(
                        watcher._route_key(), 0.0
                    )
                    cooldown = max(
                        0, int(max(watcher._blocked_until, route_until) - now_mono)
                    )
                    status = str(watcher.last_http_status or "-")
                    block = watcher.last_block_kind or "ok"
                    consec = watcher._consecutive_blocks
                    lines.append(
                        f"• {html.escape(key)[:55]} | http={status} | "
                        f"{html.escape(block)} | wait={cooldown}s | consec={consec} | "
                        f"conversion={watcher.conversion_status} | parser={watcher.parser_health}"
                    )

            await m.reply(_br("\\n".join(lines)), disable_web_page_preview=True)

        @self.dp.message(Command("dedup_clear"))
        async def dedup_clear_cmd(m: types.Message):
            if ADMIN_CHAT_ID is None or m.chat.id != ADMIN_CHAT_ID:
                await m.reply(_br("Команда доступна только админу."))
                return
            parts = (m.text or "").split(maxsplit=1)
            target = parts[1].strip().lower() if len(parts) > 1 else ""
            data = self._load_sent()
            if not target or target == "all":
                self._save_sent({})
                await m.reply(_br("Антидубликат очищен полностью."))
                return
            if target.isdigit():
                if target in data:
                    del data[target]
                    self._save_sent(data)
                    await m.reply(_br(f"Антидубликат очищен для user_id={target}."))
                else:
                    await m.reply(_br(f"Для user_id={target} записей не было."))
                return
            await m.reply(_br("Формат: /dedup_clear [all|<user_id>]"))

    async def setup_menu(self):
        await self.bot.set_my_commands(
            [
                types.BotCommand(command="start", description="Главное меню"),
                types.BotCommand(command="newsearch", description="Создать поиск"),
                types.BotCommand(command="account", description="Аккаунт"),
                types.BotCommand(command="help", description="Помощь"),
            ]
        )
        if ADMIN_CHAT_ID is not None:
            await self.bot.set_my_commands(
                [
                    types.BotCommand(command="start", description="Главное меню"),
                    types.BotCommand(command="newsearch", description="Создать поиск"),
                    types.BotCommand(command="account", description="Аккаунт"),
                    types.BotCommand(command="genkey", description="Выдать ключ"),
                    types.BotCommand(command="diag", description="Диагностика"),
                    types.BotCommand(
                        command="dedup_clear", description="Очистить антидубликат"
                    ),
                ],
                scope=types.BotCommandScopeChat(chat_id=ADMIN_CHAT_ID),
            )


# ===== main =====
async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN отсутствует в .env/Secrets")
    app = App(token)
    try:
        await app.manager.restore()
        await app.setup_menu()
        await app.dp.start_polling(app.bot)
    finally:
        await app.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
