# Деплой на Railway

Бот работает **только в одном месте**: локально (`start_bot.bat`) **или** на Railway. Два polling на одном токене → конфликт, команды не отвечают.

## 1. Остановить локальный бот

```powershell
powershell -File scripts\kill_bot.ps1
```

Не запускайте `start_bot.bat`, пока бот на Railway.

## 2. Проект Railway

1. [railway.app](https://railway.app) → проект **gastrobar** (или новый).
2. **New Service** → **GitHub Repo** → `1anyaponomareva-bit/gastrobar-event-bot`, ветка `main`.
3. Тип сервиса: **Worker** (не Web). Если Railway создал Web — в Settings → удалите домен / смените на worker-only; важна только команда старта из `railway.toml`.

## 3. Переменные окружения (Variables)

Скопируйте из локального `.env` (значения не коммитить):

| Переменная | Обязательно |
|------------|-------------|
| `TELEGRAM_BOT_TOKEN` | да |
| `GEMINI_API_KEY` | да |
| `ADMIN_ID` | да (планировщик daily/weekly) |
| `GASTROBAR_GROUP_ID` | да (публикация в группу) |
| `SPORTS_API_KEY` | да (футбол API-SPORTS) |
| `EXPECTED_BOT_USERNAME` | `gastrobar_nhatrang_bot` |
| `TIMEZONE` | `Asia/Ho_Chi_Minh` |
| `BAR_OPEN_TIME` / `BAR_CLOSE_TIME` | по желанию |
| `DAILY_POST_HOUR` | `11` |
| `WEEKLY_RADAR_DOW` / `HOUR` / `MINUTE` | `3` / `10` / `40` |
| `RADAR_WEEKLY_MAX` / `RADAR_MIN_WATCHABILITY` | по `.env.example` |
| `GASTROBAR_TV_COUNT` | `2` |

**`RUN_MODE`:** можно не задавать — на Railway включается автоматически (`RAILWAY_ENVIRONMENT`). Явно: `RUN_MODE=railway`.

Опционально: `GEMINI_MODEL=gemini-2.5-flash`, `DATABASE_PATH=/data/gastrobar_bot.sqlite3` (если подключён Volume, см. ниже).

## 4. Деплой

После `git push` на `main` Railway пересоберёт сервис. В логах должно быть:

- `RUN_MODE=railway`
- `Handlers ready: /events /daily /check`
- `Polling started`

Проверка в Telegram: `/start`, `/events`.

## 5. SQLite на Railway (опционально)

Без Volume база живёт на эфемерном диске и сбрасывается при redeploy. Для сохранения кэша афиши:

1. Service → **Volumes** → Add Volume, mount `/data`
2. Variable: `DATABASE_PATH=/data/gastrobar_bot.sqlite3`

## 6. Локальная разработка снова

1. В Railway: **Settings → Redeploy** выключить или **Pause** / удалить сервис.
2. Подождать ~30 с, `scripts\check_telegram_conflict.py` → OK.
3. `.env`: `RUN_MODE=local`
4. `start_bot.bat`

## Конфликт токена

Если `check_telegram_conflict.py` падает: где-то ещё запущен бот (второе окно, старый Railway, другой ПК). Оставьте **один** источник polling.
