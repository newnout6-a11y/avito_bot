
# -*- coding: utf-8 -*-
"""
Alert (notifier) bot for Avito Monitor
— сохраняет привязку: main_user_id -> chat_id (в файл BINDINGS_FILE)
— принимает /start <main_user_id>, показывает /id
"""

import asyncio
import logging
import os
import time

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command

from storage import load_state, save_state, update_state

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    def load_dotenv(*args, **kwargs):  # type: ignore
        return None

load_dotenv()

BINDINGS_FILE = os.getenv("BINDINGS_FILE", "user_bindings.json")
ALERT_LINKS_FILE = os.getenv("ALERT_LINKS_FILE", "alert_links.json")
SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/Multiscan_service1")

router = Router(name="alert")

SUPPORT_KB = types.InlineKeyboardMarkup(inline_keyboard=[
    [types.InlineKeyboardButton(text="Поддержка", url=SUPPORT_LINK)],
])


def _consume_link_token(token: str):
    def consume(links):
        record = links.pop(token, None)
        if record and float(record.get("expires", 0)) >= time.time():
            return int(record["user_id"])
        return None
    return update_state(ALERT_LINKS_FILE, {}, consume)

def _load_bindings():
    """Безопасная загрузка привязок"""
    try:
        raw_data = load_state(BINDINGS_FILE, {})
        if not raw_data:
            logger.info("Данные привязок пустые")
            return {}

        data = {}
        for k, v in raw_data.items():
            try:
                data[str(k)] = int(v)
            except (ValueError, TypeError) as e:
                logger.warning(f"Пропускаем некорректную запись {k}: {v}, ошибка: {e}")
                continue

        logger.info("Загружено привязок: %s", len(data))
        return data
            
    except Exception as e:
        logger.error(f"Критическая ошибка загрузки привязок: {e}")
        return {}

def _save_bindings(data):
    """Безопасное сохранение привязок"""
    try:
        # Создаем директорию если нужно
        dir_path = os.path.dirname(BINDINGS_FILE)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        
        # Конвертируем данные в правильный формат
        save_data = {}
        for k, v in data.items():
            try:
                save_data[str(k)] = int(v)
            except (ValueError, TypeError) as e:
                logger.warning(f"Пропускаем некорректную запись при сохранении {k}: {v}, ошибка: {e}")
                continue
        
        # Сохраняем с блокировкой
        save_state(BINDINGS_FILE, save_data)
        
        logger.info("Сохранено привязок: %s", len(save_data))
        
    except Exception as e:
        logger.error(f"Критическая ошибка сохранения привязок: {e}")

@router.message(Command("start"))
async def start_cmd(m: types.Message):
    """Обработка команды /start с улучшенной проверкой аргументов"""
    try:
        args = (m.text or "").split()
        main_user_id = None
        
        # Проверяем аргументы команды
        if len(args) >= 2:
            arg = args[1].strip()
            try:
                main_user_id = _consume_link_token(arg)
            except Exception as exc:
                logger.error("Ошибка проверки токена привязки: %s", exc)
        
        if main_user_id is None:
            existing_bindings = _load_bindings()
            if str(m.chat.id) in existing_bindings or m.chat.id in existing_bindings.values():
                await m.answer(
                    "✅ <b>Оповещения уже подключены</b>\n\n"
                    "Этот чат привязан для получения уведомлений. Все новые объявления по вашим поискам приходят сюда.",
                    reply_markup=SUPPORT_KB,
                    disable_web_page_preview=True,
                )
                return

            await m.answer(
                "<b>Оповещения Avito</b>\n\n"
                "Откройте основной бот и нажмите кнопку подключения оповещений.",
                reply_markup=SUPPORT_KB,
                disable_web_page_preview=True,
            )
            return
        
        # Одноразовый токен подтверждает, что ссылку создал основной бот.
        def bind(data):
            data[str(main_user_id)] = int(m.chat.id)
            return dict(data)
        update_state(BINDINGS_FILE, {}, bind)
        
        logger.info(f"Создана привязка: main_user_id={main_user_id} -> alert_chat_id={m.chat.id}")
        
        await m.answer(
            "✅ <b>Оповещения подключены</b>\n\n"
            "Новые объявления будут приходить сюда. Теперь можно вернуться в основной бот."
        )
        
    except Exception as e:
        logger.error(f"Ошибка в start_cmd: {e}")
        await m.answer(
            "Не удалось подключить оповещения. Создайте новую ссылку в основном боте.",
            reply_markup=SUPPORT_KB,
            disable_web_page_preview=True,
        )

@router.message(Command("id"))
async def id_cmd(m: types.Message):
    """Показать chat_id пользователя"""
    try:
        await m.answer(f"Ваш chat_id: <code>{m.chat.id}</code>")
    except Exception as e:
        logger.error(f"Ошибка в id_cmd: {e}")
        await m.answer("Ошибка получения ID")

@router.message(Command("status"))
async def status_cmd(m: types.Message):
    """Проверить статус привязок"""
    try:
        # Детальная диагностика
        logger.info("=== ДИАГНОСТИКА STATUS ===")
        logger.info(f"Файл привязок: {BINDINGS_FILE}")
        logger.info(f"Файл существует: {os.path.exists(BINDINGS_FILE)}")
        
        if os.path.exists(BINDINGS_FILE):
            try:
                logger.info("Файл привязок доступен")
            except Exception as read_e:
                logger.error(f"Ошибка чтения файла: {read_e}")
        
        data = _load_bindings()
        logger.info(f"Текущий chat_id: {m.chat.id} (тип: {type(m.chat.id)})")
        
        # Ищем привязку для текущего chat_id
        found_bindings = []
        for main_user_id, alert_chat_id in data.items():
            logger.info(f"Проверяем: main_user_id={main_user_id} (тип: {type(main_user_id)}), alert_chat_id={alert_chat_id} (тип: {type(alert_chat_id)})")
            try:
                if int(alert_chat_id) == int(m.chat.id):
                    found_bindings.append(main_user_id)
                    logger.info(f"Найдено совпадение: {main_user_id}")
            except Exception as comp_e:
                logger.error(f"Ошибка сравнения для {main_user_id}->{alert_chat_id}: {comp_e}")
        
        logger.info(f"Найденные привязки: {found_bindings}")
        
        if found_bindings:
            await m.answer("✅ <b>Оповещения подключены</b>\nБот готов принимать новые объявления.")
        else:
            await m.answer(
                "⚪ <b>Оповещения не подключены</b>\n"
                "Используйте кнопку подключения в основном боте.",
                reply_markup=SUPPORT_KB,
            )
            
    except Exception as e:
        logger.error(f"Критическая ошибка в status_cmd: {e}")
        await m.answer(f"Критическая ошибка проверки статуса: {e}")

@router.message(F.text)
async def echo(m: types.Message):
    """Показывает назначение бота вместо эхо-ответа."""
    try:
        await m.answer(
            "<b>Оповещения Avito</b>\n"
            "Этот чат предназначен для новых объявлений.\n\n"
            "Проверить подключение: /status"
        )
    except Exception as e:
        logger.error(f"Ошибка в echo: {e}")

async def main():
    """Главная функция запуска бота"""
    try:
        token = os.getenv("ALERT_BOT_TOKEN")
        
        if not token:
            logger.error("ALERT_BOT_TOKEN не задан в переменных окружения!")
            print("ОШИБКА: ALERT_BOT_TOKEN не задан!")
            print("Добавьте токен в Secrets или .env файл")
            return
        
        logger.info("Запуск alert-бота...")
        
        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp = Dispatcher()
        dp.include_router(router)
        
        # Устанавливаем команды бота
        await bot.set_my_commands([
            types.BotCommand(command="start", description="Открыть бот"),
            types.BotCommand(command="status", description="Проверить подключение"),
        ])
        
        logger.info("Alert-бот запущен успешно")
        await dp.start_polling(bot)
        
    except Exception as e:
        logger.error(f"Критическая ошибка в main: {e}")
        print(f"КРИТИЧЕСКАЯ ОШИБКА: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Alert-бот остановлен")
        print("Alert-бот остановлен")
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {e}")
        print(f"НЕОЖИДАННАЯ ОШИБКА: {e}")


def run() -> None:
    asyncio.run(main())
