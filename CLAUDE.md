# CLAUDE.md — Инструкции для Claude Code

⚠️ Запуск бота без флагов = публикация в боевую группу. Без явной команды владельца в группу ничего не публиковать. Текущее состояние (в т.ч. пауза автопостинга с 2026-08-22) — в JOURNAL.md.

## Проект
Telegram-боты для группы "РУССКИЙ ДАНАНГ" (@rus_danang, chat_id -100XXXXXXXXXX, форум с темами).

- **weather_bot.py** — утренний дайджест: погода, AQI, курсы валют, топливо, золото → General чат (без thread_id)
- **news_bot.py** — новости Дананга/Вьетнама, перевод через `claude -p`, → тема "Новости" (thread_id 1451)
- **read_history.py** — чтение истории треда через Telethon (user account), наполнение трекера дедупликации
- **vietnamese_bot.py** — урок вьетнамского дня из курса на 365 дней → тема уроков
- **vietnamese_lesson_builder.py** — генератор уроков партиями (запускается вручную, не из cron)
- **expat_guide_bot.py** — еженедельный материал гайда экспата → тема гайда
- **expat_guide_builder.py** — генератор материалов гайда (запускается вручную)
- **guide_verify_apply.py** — применение фактчека к материалам гайда
- **healthcheck.py** — сторож молчания рубрик и запаса контента, алерт в служебный чат

Общие модули:
- **telegram_sender.py** — отправка в Telegram: retry/429, unknown-outcome, хак General-чата, тестовый чат, heartbeats.json
- **rich_render.py** — конвертер плоского поста в Rich HTML для новостей, уроков и гайда (погода строит rich сама, см. `format_post_rich` в weather_bot.py)
- **article_image.py** — скачивание картинки статьи во временный файл, удаляется сразу после отправки
- **dedup.py** — трёхуровневая дедупликация новостей
- **facebook_poster.py** — кросспост дайджеста и новостей на Facebook-страницу

## Архитектура
- Бот-аккаунт (@rus_danang_bot) — только для постинга
- User-аккаунт (Telethon, reader_session.session) — только для чтения истории. Никогда не постить с него
- Конфиг: `config.json` (секреты, НЕ коммитить)
- Гайд экспата можно выключить: `expat_guide.enabled: false` в config.json — бот ничего не публикует, healthcheck.py рубрику не проверяет
- Трекер дубликатов: `danang-news-posted.json` (последние 1000 записей); битый файл сохраняется как `<файл>.corrupt-<время>`, бот выходит с кодом 1.
- Расписание: cron (сервер в UTC, время Дананга = UTC+7)

## Ключевые правила
- **Формат новостей**: эмодзи + ЗАГОЛОВОК КАПСОМ + 3-4 абзаца с эмодзи + "🌴 Для экспатов:" + "📰 Источник: URL" + 5 хештегов (#Дананг #Danang #жизньвДананге #экспатДананг #тема) + #Vietnam<год>. Публикуется rich-статьёй (`sendRichMessage`, `rich_render.py`) с заглавной картинкой статьи (`article_image.py` — временный файл, удаляется после отправки); картинку не принял Telegram → rich без картинки; rich не принят → обычный `sendMessage` со ссылкой (превью). Проверка перед отправкой: заголовок капсом, «🌴 Для экспатов:», точная строка «📰 Источник: URL», последняя строка оканчивается на #Vietnam<год>, нет реплик модели, не длиннее 4000 символов; не прошёл — следующая статья-кандидат (до трёх).
- **Мета-комментарии модели**: любой текст от `claude CLI` перед публикацией — только через `clean_ai_output()` и проверку формата; мета-комментарии модели в посты не пропускать.
- **Дедупликация** (3 уровня, модуль `dedup.py`): L1 URL exact match → L2 Jaccard/Containment английских заголовков без суффикса издания (порог 0.35) → L3 entity fingerprint (числа + латинские слова + URL slug, без суффикса издания, кросс-языковая RU↔EN, min_overlap=4, min_ratio=0.4). L2/L3 сравнивают только последние 30 дней трекера, L1 — без ограничения по времени. Трекер (`danang-news-posted.json`) хранит последние 1000 записей. URL резолв (googlenewsdecoder) → дедуп → фильтр по возрасту (3 дня) → ранжирование.
- **Анти-бан Telethon**: `FloodWaitError` → выход с кодом 1 без ожидания (cron-джоб; Telegram может попросить ждать часами), чтение ограничено 200 сообщениями за проход, сессия переиспользуется.
- **Отправка в Telegram** (`telegram_sender.py`): Повтор (до трёх попыток) — только при 429 и когда соединение не удалось установить (DNS, отказ в соединении, connect-таймаут). Обрыв после отправки запроса, таймаут чтения, ошибка SSL или редиректов, 5xx, неразбираемый ответ — «исход неизвестен» (`SendOutcomeUnknown`): без повтора и без запасного варианта, state не двигается, код выхода 1; новость с таким исходом пишется в трекер с `uncertain`. Публикующие боты берут `flock` (weather_bot, news_bot, vietnamese_bot, expat_guide_bot); news_bot и read_history.py делят один лок на файл трекера; `vietnamese_lesson_builder.py` лочит `vietnamese_lessons.json`.
- **thread_id=1** не работает в форумных группах — для General не передавать message_thread_id
- **Флаги и коды выхода**: weather_bot: `--dry-run --test --force --plain --rich-list`; news_bot: `--test --force --init`; vietnamese_bot и expat_guide_bot: `--dry-run --test --force --init --plain`; healthcheck: `--dry-run --always`; read_history: без аргументов; vietnamese_lesson_builder: `--month N --day N[,N] --allow-published --force --preview --limit N`. Коды выхода: 0 — успех, штатный пропуск или лок занят другим запуском; 1 — сбой, исход отправки неизвестен, нет state/трекера без `--init` (у билдера уроков — ещё и занятый лок); 2 — неизвестный флаг, а у уроков и гайда также «опубликовано, но state не сохранён».

## Команды
```bash
# Без отправки (ничего не публикует и не меняет state)
./venv/bin/python3 weather_bot.py --dry-run
./venv/bin/python3 vietnamese_bot.py --dry-run
./venv/bin/python3 expat_guide_bot.py --dry-run
./venv/bin/python3 healthcheck.py --dry-run

# В тестовый чат (telegram.test_chat_id), state не двигается
./venv/bin/python3 weather_bot.py --test
./venv/bin/python3 vietnamese_bot.py --test
./venv/bin/python3 expat_guide_bot.py --test
./venv/bin/python3 news_bot.py --test      # тратит лимиты claude

# ⚠️ Без флагов — публикация в боевую группу. Неизвестный флаг — выход с кодом 2.
# Нет state-файла или трекера — бот не стартует; --init только для осознанного старта с нуля.

# Синхронизация трекера из истории треда (Telethon, только чтение)
./venv/bin/python3 read_history.py

# Установка (печатает строки cron, сам crontab не трогает)
chmod +x setup.sh && ./setup.sh

# Логи
tail -f logs/*.log
```

## Зависимости
Python 3.10+, requests, beautifulsoup4, telethon, googlenewsdecoder + Claude Code CLI (`claude` в PATH, установлен и залогинен под тем пользователем/cron, от которого идёт запуск)

## Журнал
Текущее состояние и история изменений — в JOURNAL.md
