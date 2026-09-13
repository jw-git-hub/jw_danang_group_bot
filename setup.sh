#!/bin/bash
export PATH="$HOME/.local/bin:$PATH"
# Setup script for Danang Bots on home server
# Usage: chmod +x setup.sh && ./setup.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "=== Danang Bots Setup ==="
echo "Directory: $SCRIPT_DIR"

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

# Show crontab entries
# Сервер живёт в UTC. Все строки ниже — в UTC, местное время Дананга (UTC+7) указано в комментарии.
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python3"
echo "=== Add these lines to crontab (crontab -e) ==="
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
echo "# Expat Guide Bot — воскресенье 11:00 Дананга"
echo "0 4 * * 0 cd $SCRIPT_DIR && $VENV_PYTHON expat_guide_bot.py >> logs/expat_guide.log 2>&1"
echo ""
echo "=== Setup complete ==="
