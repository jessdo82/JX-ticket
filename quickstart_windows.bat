\
@echo off
cd /d %~dp0
where python >nul 2>&1 || (echo 請先安裝 Python 3 && pause && exit /b)
if not exist .venv (python -m venv .venv)
call .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
if exist .env (for /f "usebackq tokens=1,2 delims==" %%a in (".env") do (set %%a=%%b))
python monitor.py
pause
