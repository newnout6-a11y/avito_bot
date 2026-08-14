# Avito Monitor

Telegram-бот для мониторинга новых объявлений Avito через JSON API и отправки
уведомлений в отдельный alert-бот.

## Запуск

1. Установить зависимости: `python -m pip install -e .`
2. Создать `.env` на основе локальной конфигурации и задать `BOT_TOKEN`.
3. Для уведомлений задать `ALERT_BOT_TOKEN` и `ALERT_BOT_USERNAME`.
4. Запустить основной бот: `avito-monitor`.
5. Запустить alert-бот в отдельном процессе: `avito-alert`.

По умолчанию данные хранятся в `data/avito_monitor.sqlite3`. Старые относительные
JSON-файлы (`accounts.json`, `subscriptions.json` и другие) импортируются в базу
при первом чтении. Для разовой совместимости можно установить
`STORAGE_BACKEND=json`.

Основные настройки находятся в [avito_settings.py](avito_settings.py).

## Telegram UI

Кнопки используют цвета Bot API (`primary`, `success`, `danger`). Уведомления
содержат кнопки для открытия объявления и копирования ссылки.

Дополнительные возможности настраиваются через окружение:

- `ALERT_MESSAGE_EFFECT_ID` — эффект сообщения для уведомлений в личном чате;
- `TELEGRAM_PRIMARY_BUTTON_ICON_ID` — custom emoji для основных кнопок;
- `TELEGRAM_SUCCESS_BUTTON_ICON_ID` — custom emoji для подтверждений;
- `TELEGRAM_DANGER_BUTTON_ICON_ID` — custom emoji для опасных действий.

Custom emoji на кнопках отображаются, когда бот имеет право использовать
соответствующие ID (Premium у владельца либо дополнительные username в Fragment).
Если Telegram отклоняет эффект сообщения, уведомление автоматически отправляется
повторно без эффекта.

## Разработка

Установить проект вместе с dev-зависимостями: `uv sync --dev`.

- Проверка Ruff: `uv run ruff check .`
- Автоисправление безопасных замечаний: `uv run ruff check . --fix`
- Тесты: `uv run python -m unittest -v`
