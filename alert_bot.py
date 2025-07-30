
# -*- coding: utf-8 -*-
"""
Alert (notifier) bot for Avito Monitor
— сохраняет привязку: main_user_id -> chat_id (в файл BINDINGS_FILE)
— принимает /start <main_user_id>, показывает /id
"""

import os
import json
import asyncio
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    def load_dotenv(*args, **kwargs):  # type: ignore
        return None

BINDINGS_FILE = os.getenv("BINDINGS_FILE", "user_bindings.json")
SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/Multiscan_service1")

router = Router(name="alert")

def _load_bindings():
    if not os.path.exists(BINDINGS_FILE):
        return {}
    try:
        with open(BINDINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
            return {str(k): int(v) for k, v in data.items()}
    except Exception:
        return {}

def _save_bindings(data):
    os.makedirs(os.path.dirname(BINDINGS_FILE) or ".", exist_ok=True)
    with open(BINDINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

@router.message(Command("start"))
async def start_cmd(m: types.Message):
    args = (m.text or "").split(maxsplit=1)
    main_user_id = None
    if len(args) == 2 and args[1].lstrip("-").isdigit():
        main_user_id = int(args[1])
    if main_user_id is None:
        await m.answer(
            "Это бот-оповещатель. Запустите меня кнопкой из основного бота после активации ключа.\n"
            "Поддержка: {}".format(SUPPORT_LINK),
            disable_web_page_preview=True
        )
        return
    # save binding
    data = _load_bindings()
    data[str(main_user_id)] = int(m.chat.id)
    _save_bindings(data)
    await m.answer("Привязка выполнена ✅\nВаш chat_id: <code>{}</code>\nТеперь вернитесь в основной бот и создайте поиск.".format(m.chat.id))

@router.message(Command("id"))
async def id_cmd(m: types.Message):
    await m.answer(f"Ваш chat_id: <code>{m.chat.id}</code>")

@router.message(F.text)
async def echo(m: types.Message):
    # чтобы бот был «живой» при тестах
    await m.answer("Оповещатель активен. Ваш chat_id: <code>{}</code>.\n/start <main_user_id> — привязка.".format(m.chat.id))

async def main():
    load_dotenv()
    token = os.getenv("ALERT_BOT_TOKEN")
    if not token:
        raise RuntimeError("ALERT_BOT_TOKEN не задан")
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
