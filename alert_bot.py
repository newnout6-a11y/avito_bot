
# -*- coding: utf-8 -*-
"""
Alert (notifier) bot for Avito Monitor
— сохраняет привязку: main_user_id -> chat_id (в файл BINDINGS_FILE)
— принимает /start <main_user_id>, показывает /id
"""

import os
import json
import asyncio
import logging
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    def load_dotenv(*args, **kwargs):  # type: ignore
        return None

BINDINGS_FILE = os.getenv("BINDINGS_FILE", "user_bindings.json")
SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/Multiscan_service1")

router = Router(name="alert")

def _load_bindings():
    """Безопасная загрузка привязок"""
    try:
        if not os.path.exists(BINDINGS_FILE):
            logger.info(f"Файл привязок {BINDINGS_FILE} не существует, создаем пустой")
            # Создаем пустой файл
            _save_bindings({})
            return {}
        
        with open(BINDINGS_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                logger.info("Файл привязок пустой")
                return {}
            
            raw_data = json.loads(content)
            if not raw_data:
                logger.info("Данные привязок пустые")
                return {}
                
            # Конвертируем в правильный формат
            data = {}
            for k, v in raw_data.items():
                try:
                    data[str(k)] = int(v)
                except (ValueError, TypeError) as e:
                    logger.warning(f"Пропускаем некорректную запись {k}: {v}, ошибка: {e}")
                    continue
            
            logger.info(f"Загружены привязки: {data}")
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
        temp_file = BINDINGS_FILE + ".tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(save_data, f, ensure_ascii=False, indent=2)
        
        # Атомарно перемещаем временный файл
        os.replace(temp_file, BINDINGS_FILE)
        
        logger.info(f"Привязки успешно сохранены: {save_data}")
        
        # Проверяем, что файл действительно сохранился
        if os.path.exists(BINDINGS_FILE):
            with open(BINDINGS_FILE, "r", encoding="utf-8") as f:
                verify_data = json.load(f)
                logger.info(f"Проверка сохранения: {verify_data}")
        else:
            logger.error("Файл не был создан!")
            
    except Exception as e:
        logger.error(f"Критическая ошибка сохранения привязок: {e}")
        # Удаляем временный файл если он остался
        temp_file = BINDINGS_FILE + ".tmp"
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except:
                pass

@router.message(Command("start"))
async def start_cmd(m: types.Message):
    """Обработка команды /start с улучшенной проверкой аргументов"""
    try:
        args = (m.text or "").split()
        main_user_id = None
        
        # Проверяем аргументы команды
        if len(args) >= 2:
            arg = args[1].strip()
            # Убираем возможные префиксы и проверяем, что это число
            if arg.lstrip("-").isdigit():
                main_user_id = int(arg)
            else:
                logger.warning(f"Некорректный аргумент start: {arg}")
        
        if main_user_id is None:
            await m.answer(
                "Это бот-оповещатель для Avito Monitor.\n\n"
                "Для привязки используйте кнопку из основного бота после активации ключа.\n\n"
                f"Поддержка: {SUPPORT_LINK}",
                disable_web_page_preview=True
            )
            return
        
        # Сохраняем привязку
        data = _load_bindings()
        data[str(main_user_id)] = int(m.chat.id)
        _save_bindings(data)
        
        logger.info(f"Создана привязка: main_user_id={main_user_id} -> alert_chat_id={m.chat.id}")
        logger.info(f"Все привязки после сохранения: {data}")
        
        await m.answer(
            "✅ Привязка выполнена успешно!\n\n"
            f"Ваш chat_id: <code>{m.chat.id}</code>\n"
            f"Основной пользователь: <code>{main_user_id}</code>\n\n"
            "Теперь вернитесь в основной бот и создайте поиск."
        )
        
    except Exception as e:
        logger.error(f"Ошибка в start_cmd: {e}")
        await m.answer(
            "Произошла ошибка при обработке команды. Попробуйте еще раз или обратитесь в поддержку.\n\n"
            f"Поддержка: {SUPPORT_LINK}",
            disable_web_page_preview=True
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
        logger.info(f"=== ДИАГНОСТИКА STATUS ===")
        logger.info(f"Файл привязок: {BINDINGS_FILE}")
        logger.info(f"Файл существует: {os.path.exists(BINDINGS_FILE)}")
        
        if os.path.exists(BINDINGS_FILE):
            try:
                with open(BINDINGS_FILE, "r", encoding="utf-8") as f:
                    raw_content = f.read()
                logger.info(f"Содержимое файла: '{raw_content}'")
                logger.info(f"Размер файла: {len(raw_content)} символов")
            except Exception as read_e:
                logger.error(f"Ошибка чтения файла: {read_e}")
        
        data = _load_bindings()
        logger.info(f"Загружены привязки: {data} (тип: {type(data)})")
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
            bindings_text = "\n".join([f"• Main user ID: <code>{uid}</code>" for uid in found_bindings])
            await m.answer(f"✅ Активные привязки:\n{bindings_text}")
        else:
            # Показываем детальную диагностику
            status_lines = [
                f"❌ Привязки для chat_id {m.chat.id} не найдены",
                f"",
                f"📋 Диагностика:",
                f"• Файл существует: {os.path.exists(BINDINGS_FILE)}",
                f"• Всего привязок: {len(data)}",
            ]
            
            if data:
                status_lines.append(f"• Все привязки:")
                for mid, aid in data.items():
                    status_lines.append(f"  {mid} → {aid}")
            else:
                status_lines.append(f"• Файл пустой или данные не загружены")
            
            await m.answer("\n".join(status_lines))
            
    except Exception as e:
        logger.error(f"Критическая ошибка в status_cmd: {e}")
        await m.answer(f"Критическая ошибка проверки статуса: {e}")

@router.message(F.text)
async def echo(m: types.Message):
    """Эхо-обработчик для проверки работы бота"""
    try:
        await m.answer(
            f"🤖 Бот-оповещатель активен\n"
            f"Ваш chat_id: <code>{m.chat.id}</code>\n\n"
            f"Команды:\n"
            f"• /start <main_user_id> — привязка\n"
            f"• /id — показать ваш chat_id\n"
            f"• /status — проверить привязки"
        )
    except Exception as e:
        logger.error(f"Ошибка в echo: {e}")

async def main():
    """Главная функция запуска бота"""
    try:
        load_dotenv()
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
            types.BotCommand(command="start", description="Привязать к основному боту"),
            types.BotCommand(command="id", description="Показать ваш chat_id"),
            types.BotCommand(command="status", description="Проверить привязки"),
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
