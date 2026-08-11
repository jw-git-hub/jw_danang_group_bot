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

# Show crontab entries
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python3"
echo "=== Add these lines to crontab (crontab -e) ==="
echo ""
echo "# Danang Weather Bot — daily at 7:00"
echo "0 7 * * * cd $SCRIPT_DIR && $VENV_PYTHON weather_bot.py >> logs/weather.log 2>&1"
echo ""
echo "# Danang News Bot — 2x daily at 9:00 and 19:00"
echo "0 9 * * * cd $SCRIPT_DIR && $VENV_PYTHON news_bot.py >> logs/news.log 2>&1"
echo "0 19 * * * cd $SCRIPT_DIR && $VENV_PYTHON news_bot.py >> logs/news.log 2>&1"
echo ""
echo "# Vietnamese Lesson Bot — daily at 12:00 Danang (05:00 UTC)"
echo "0 5 * * * cd $SCRIPT_DIR && $VENV_PYTHON vietnamese_bot.py >> logs/vietnamese.log 2>&1"
echo ""
echo "# Expat Guide Bot — Sunday at 11:00 Danang (04:00 UTC Sun)"
echo "0 4 * * 0 cd $SCRIPT_DIR && $VENV_PYTHON expat_guide_bot.py >> logs/expat_guide.log 2>&1"
echo ""
echo "=== Setup complete ==="
