@echo off
cd /d "C:\Users\Nikita Ponomarev\Downloads\GASTROBAR_bot"
call .venv\Scripts\activate
echo.
echo ============================================================
echo  ЛОКАЛЬНЫЙ ЗАПУСК. Если бот на Railway - НЕ запускайте это.
echo  См. RAILWAY.md. Конфликт = /events не отвечает.
echo ============================================================
echo.
echo === Останов старых python main.py и очистка __pycache__ ===
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\kill_bot.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\purge_pycache.ps1
if exist bot.lock del /f bot.lock
echo.
echo === Build ===
.venv\Scripts\python.exe -B -c "from runtime_messages import BOT_BUILD_ID; print('BOT_BUILD_ID=', BOT_BUILD_ID)"
echo.
echo === Проверка Telegram (один getUpdates) ===
.venv\Scripts\python.exe -B scripts\check_telegram_conflict.py
if errorlevel 1 (
  echo.
  echo Конфликт: drugoj klient derzhit getUpdates.
  echo Zakroyte VSE okna start_bot.bat. Esli ne pomoglo - Revoke token v BotFather.
  echo.
  echo Ждём 45 сек, пока Telegram отпустит сессию…
  timeout /t 45 /nobreak
  .venv\Scripts\python.exe -B scripts\check_telegram_conflict.py
  if errorlevel 1 (
    echo.
    echo Конфликт остался. Только ОДИН источник бота: локально ИЛИ Railway.
    echo Не запускайте start_bot.bat, пока check не покажет OK.
    pause
    exit /b 1
  )
)
echo.
.venv\Scripts\python.exe -B main.py
pause
