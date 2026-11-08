#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "Tworzę środowisko Pythona..."
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
fi

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Uzupełnij DISCORD_TOKEN w pliku .env i uruchom ponownie."
    exit 1
fi

exec .venv/bin/python bot.py
