# CLAUDE.md — Инструкции для Claude Code

## Проект
Два Telegram-бота для группы "РУССКИЙ ДАНАНГ" (@rus_danang, chat_id -100XXXXXXXXXX, форум с темами).
- **weather_bot.py** — погода + AQI → General чат (без thread_id)
- **news_bot.py** — новости Дананга/Вьетнама, перевод через `claude -p`, → тема "Новости" (thread_id 1451)
- **read_history.py** — чтение истории треда через Telethon (user account), наполнение трекера дедупликации

## Архитектура
- Бот-аккаунт (@rus_danang_bot) — только для постинга
- User-аккаунт (Telethon, reader_session.session) — только для чтения истории. Никогда не постить с него
- Конфиг: `config.json` (секреты, НЕ коммитить)
- Трекер дубликатов: `danang-news-posted.json` (последние 200 записей)
- Расписание: cron (сервер в UTC, время Дананга = UTC+7)

## Ключевые правила
- **Формат новостей**: эмодзи + ЗАГОЛОВОК КАПСОМ + 3-4 абзаца с эмодзи + "🌴 Для экспатов:" + "📰 Источник: URL" + 5 хештегов (#Дананг #Danang #жизньвДананге #экспатДананг #тема) + #Vietnam2026. Без картинок (превью из ссылки). sendMessage, НЕ sendPhoto.
- **Дедупликация** (3 уровня, модуль `dedup.py`): L1 URL exact match → L2 Jaccard/Containment английских заголовков (порог 0.35) → L3 entity fingerprint (числа + латинские слова + URL slug, кросс-язычная RU↔EN, min_overlap=3, min_ratio=0.4). URL резолв (googlenewsdecoder) → дедуп → фильтр по возрасту (3 дня) → ранжирование
- **Анти-бан Telethon**: FloodWaitError обрабатывается, лимит 200 сообщений, сессия переиспользуется
- **Telegram 429**: итеративный retry (макс 3 попытки) с ожиданием retry_after
- **thread_id=1** не работает в форумных группах — для General не передавать message_thread_id

## Команды
```bash
# Тест погоды
./venv/bin/python3 weather_bot.py

# Тест новостей
./venv/bin/python3 news_bot.py

# Синхронизация трекера
./venv/bin/python3 read_history.py

# Установка
chmod +x setup.sh && ./setup.sh

# Логи
tail -f logs/weather.log logs/news.log logs/reader.log
```

## Зависимости
requests, beautifulsoup4, telethon, googlenewsdecoder + Claude Code CLI (`claude` в PATH)

## Журнал
Текущее состояние и история изменений — в JOURNAL.md
