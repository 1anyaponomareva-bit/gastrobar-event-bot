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
  echo КОНФЛИКТ: getUpdates занят — чаще всего бот уже на Railway.
  echo НЕ запускайте start_bot.bat. Закройте это окно.
  echo Локальный main.py будет мешать облаку и слать вам лишние предупреждения.
  echo Проверка: check_conflict.bat когда Railway один.
  pause
  exit /b 1
)
echo.
.venv\Scripts\python.exe -B main.py
pause
