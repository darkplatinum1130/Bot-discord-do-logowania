@echo off
cd /d "%~dp0"
if not exist .venv (
    echo Tworze srodowisko Pythona...
    python -m venv .venv || goto :error
    .venv\Scripts\python -m pip install -r requirements.txt || goto :error
)
if not exist .env (
    copy .env.example .env >nul
    echo Uzupelnij DISCORD_TOKEN w pliku .env i uruchom ponownie.
    notepad .env
    pause
    exit /b
)
.venv\Scripts\python bot.py
pause
exit /b

:error
echo Cos poszlo nie tak. Sprawdz, czy masz zainstalowanego Pythona 3.10+.
pause
