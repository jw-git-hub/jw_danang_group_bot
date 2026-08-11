# Danang Bots — Telegram автопостинг для экспатов

Два бота для автоматической публикации в Telegram-группу РУССКИЙ ДАНАНГ:
- **weather_bot.py** — ежедневный прогноз погоды + AQI
- **news_bot.py** — новости о Дананге с переводом через Claude Code

## Требования

- Python 3.9+
- Claude Code (установлен и авторизован на сервере)
- Доступ к интернету

## Структура

```
danang-bots/
├── config.json              # Токены и настройки
├── weather_bot.py           # Бот погоды (без AI, чистый Python)
├── news_bot.py              # Бот новостей (использует claude -p для перевода)
├── requirements.txt         # Python зависимости (requests + beautifulsoup4)
├── setup.sh                 # Скрипт установки
├── danang-news-posted.json  # Трекер дубликатов (создаётся автоматически)
└── logs/                    # Логи
```

## Быстрый старт

```bash
# 1. Скопировать на сервер
scp -r danang-bots/ user@server:/home/user/

# 2. Установить
cd danang-bots
chmod +x setup.sh
./setup.sh

# 3. Проверить что claude работает
claude -p "Скажи привет"

# 4. Тестовый запуск
./venv/bin/python3 weather_bot.py
./venv/bin/python3 news_bot.py

# 5. Настроить cron (setup.sh покажет строки)
crontab -e
```

## Как работает

### weather_bot.py (без AI)
1. Получает данные с Open-Meteo (бесплатно) и AQICN
2. Форматирует пост по шаблону
3. Отправляет в Telegram через Bot API

### news_bot.py (через Claude Code)
1. Проверяет "окно" — утреннее (9–18) или вечернее (19–23). Если пост уже был — пропускает.
2. Ищет новости через Google News RSS.
3. Фильтрует дубликаты (Jaccard + Containment > 35%).
4. Извлекает OG-image и текст статьи.
5. Вызывает `claude -p "промпт"` для перевода и форматирования.
6. Публикует в Telegram (фото + caption).
7. Сохраняет в трекер.

## Расписание cron

```
# Погода — каждый день в 7:00
0 7 * * * cd /path/to/danang-bots && ./venv/bin/python3 weather_bot.py >> logs/weather.log 2>&1

# Новости — 2 раза в день (утро и вечер)
0 9 * * *  cd /path/to/danang-bots && ./venv/bin/python3 news_bot.py >> logs/news.log 2>&1
0 19 * * * cd /path/to/danang-bots && ./venv/bin/python3 news_bot.py >> logs/news.log 2>&1
```

## Конфигурация (config.json)

| Поле | Описание |
|------|----------|
| `telegram.bot_token` | Токен Telegram бота |
| `telegram.chat_id` | ID чата группы |
| `telegram.news_thread_id` | Тред "Новости" (1451) |
| `telegram.weather_thread_id` | Общий чат (1) |
| `weather.aqi_token` | Токен AQICN |
