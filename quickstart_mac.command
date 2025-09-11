#!/bin/bash
cd "$(dirname "$0")"
command -v python3 >/dev/null 2>&1 || { echo "請先安裝 Python 3"; read -n 1; exit 1; }
python3 -m venv .venv 2>/dev/null || true
source .venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install --with-deps chromium
[ -f ".env" ] && export $(grep -v '^#' .env | xargs)
python3 monitor.py
read -n 1 -s -r -p "按任意鍵關閉..."
