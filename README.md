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

Основные настройки находятся в [avito_settings.py](avito_settings.py). Тесты:
`python -m unittest -v`.
