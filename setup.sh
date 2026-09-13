#!/bin/bash
export PATH="$HOME/.local/bin:$PATH"
# Setup script for Danang Bots on home server
# Usage: chmod +x setup.sh && ./setup.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "=== Danang Bots Setup ==="
echo "Directory: $SCRIPT_DIR"

# Требуется Python 3.10+ (на 3.9 модули импортируются, но в работе это не проверялось).
PY_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 10) else 0)' 2>/dev/null || echo 0)"
if [ "$PY_OK" != "1" ]; then
    echo "⚠️  Нужен python3 версии 3.10+ (сейчас: $(python3 -V 2>&1)) — часть кода использует синтаксис 3.10."
fi

# Create logs directory
mkdir -p "$SCRIPT_DIR/logs"

# Create virtual environment
echo "Creating Python virtual environment..."
python3 -m venv "$SCRIPT_DIR/venv"
source "$SCRIPT_DIR/venv/bin/activate"

# Install dependencies
echo "Installing dependencies..."
pip install -r "$SCRIPT_DIR/requirements.txt"

echo ""
echo "=== Dependencies installed ==="
echo ""

# Check claude is installed
if ! command -v claude &> /dev/null; then
    echo "⚠️  Claude Code not found! Install it: https://docs.anthropic.com/en/docs/claude-code"
    echo ""
fi

# Create config.json from template if it doesn't exist yet, and lock it down
if [ ! -f "$SCRIPT_DIR/config.json" ]; then
    cp "$SCRIPT_DIR/config.example.json" "$SCRIPT_DIR/config.json"
    echo "Created config.json from config.example.json — заполните секреты перед запуском."
else
    echo "config.json уже существует — не трогаем."
fi
chmod 600 "$SCRIPT_DIR/config.json"

# logrotate.conf хранит абсолютный путь к логам. Если репозиторий склонирован
# не в тот каталог, на который рассчитан файл в git, — печатаем готовую
# команду, которая поправит путь на месте.
LOGROTATE_PATH="$(grep -oE '^[^ ]+/logs/\*\.log' "$SCRIPT_DIR/logrotate.conf" 2>/dev/null | head -1 | sed 's#/logs/\*\.log##')"
if [ -n "$LOGROTATE_PATH" ] && [ "$LOGROTATE_PATH" != "$SCRIPT_DIR" ]; then
    echo ""
    echo "⚠️  logrotate.conf указывает на $LOGROTATE_PATH, а проект лежит в $SCRIPT_DIR — путь нужно поправить:"
    echo "  sed -i 's#$LOGROTATE_PATH#$SCRIPT_DIR#' \"$SCRIPT_DIR/logrotate.conf\""
fi

# Show crontab entries
# Сервер живёт в UTC. Все строки ниже — в UTC, местное время Дананга (UTC+7) указано в комментарии.
echo ""
[ "$(date +%z)" = "+0000" ] || echo "⚠️ Сервер не в UTC ($(date +%Z)) — строки ниже рассчитаны на UTC, пересчитайте часы."
echo "⚠️  Не включайте эти строки, пока боты работают на другом сервере — оба начнут постить в группу."
echo ""
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python3"
echo "=== Add these lines to crontab (crontab -e) ==="
echo ""
echo "PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
echo ""
echo "# Danang Weather Bot — 07:00 Дананга"
echo "0 0 * * * cd $SCRIPT_DIR && $VENV_PYTHON weather_bot.py >> logs/weather.log 2>&1"
echo ""
echo "# History sync — 08:00 Дананга"
echo "0 1 * * * cd $SCRIPT_DIR && $VENV_PYTHON read_history.py >> logs/reader.log 2>&1"
echo ""
echo "# Danang News Bot — 09:00 и 19:00 Дананга"
echo "0 2 * * * cd $SCRIPT_DIR && $VENV_PYTHON news_bot.py >> logs/news.log 2>&1"
echo "0 12 * * * cd $SCRIPT_DIR && $VENV_PYTHON news_bot.py >> logs/news.log 2>&1"
echo ""
echo "# Vietnamese Lesson Bot — 12:00 Дананга"
echo "0 5 * * * cd $SCRIPT_DIR && $VENV_PYTHON vietnamese_bot.py >> logs/vietnamese.log 2>&1"
echo ""
echo "# Expat Guide Bot — воскресенье 11:10 Дананга"
echo "10 4 * * 0 cd $SCRIPT_DIR && $VENV_PYTHON expat_guide_bot.py >> logs/expat_guide.log 2>&1"
echo ""
echo "# Healthcheck — сторож автопостинга, 20:30 Дананга (после публикаций дня)"
echo "30 13 * * * cd $SCRIPT_DIR && $VENV_PYTHON healthcheck.py >> logs/healthcheck.log 2>&1"
echo ""
echo "# Ротация логов — воскресенье 21:00 Дананга"
echo "0 14 * * 0 /usr/sbin/logrotate -s \$HOME/.danang-logrotate.state $SCRIPT_DIR/logrotate.conf"
echo ""
echo "=== Setup complete ==="
