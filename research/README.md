# research/

Одноразовые live-пробы и заметки по анти-бот защите Avito (Qrator/PoW),
прокси-ландшафту и sitemap-каналу. Не часть рантайма бота — держим для истории
замеров и как отправную точку при следующем изменении транспортного слоя.

## Содержимое

- `probe_*.py` — скрипты живых замеров; рядом лежит соответствующий `*.json` с результатом.
- `proxy_sweep.py` — массовая проверка прокси-списка (`--out` в json).
- `challenge_page.html` — захваченная страница firewall-challenge, из неё выведена
  механика `avito_pow.py`.
- `RESEARCH_PRO.md`, `RESEARCH_PROXIES.md`, `PROXY_LIST_VERDICT.md`,
  `proxy_landscape_report.md` — синтез по итогам проб.

## Запуск

Скрипты импортируют модули бота, поэтому запускать из корня репозитория с корнем в
`PYTHONPATH`:

```bash
PYTHONPATH=. python research/probe_stability.py
```

Рабочий инструмент сбора прокси (`proxy_harvest.py`) остался в корне — его тянет
`test_regressions.py`.
