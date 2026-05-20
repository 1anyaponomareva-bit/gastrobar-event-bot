# Деплой на Railway

## Это не сайт Gastrobar на Vercel

Отдельный проект — **сайт Gastrobar** (GitHub + **Vercel**) — **не этот репозиторий** и **не деплоится через Railway**.

Здесь только **Telegram-бот**: репозиторий **`gastrobar-event-bot`** и один **Worker** на Railway с `TELEGRAM_BOT_TOKEN`. Название **проекта** в Railway может быть любым (например `astonishing-smile`).

---

Бот работает **только в одном месте**: локально (`start_bot.bat`) **или** на Railway. Два процесса с одним токеном дерутся за `getUpdates` — команды в Telegram могут не доходить.

## 1. Остановить локальный бот

```powershell
powershell -File scripts\kill_bot.ps1
```

Не запускайте `start_bot.bat`, пока бот на Railway.

## 2. Проект Railway

1. [railway.app](https://railway.app) → любой ваш проект (имя карты в UI не привязано к сайту на Vercel).
2. **New Service** → **GitHub Repo** → **`1anyaponomareva-bit/gastrobar-event-bot`**, ветка **`main`** (именно репозиторий бота, не репо сайта).
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

**`RUN_MODE`:** на Railway **не ставьте `local`** — бот уйдёт в «локальный» режим и будет конфликтовать с самим собой. Лучше: `RUN_MODE=railway` или вообще не задавать (авто по `RAILWAY_ENVIRONMENT`).

**Деплой и `TelegramConflict`:** при перезапуске контейнера старый процесс кратко держит `getUpdates`. Бот на Railway **ждёт до ~3 мин** (по умолчанию 40×5 с) и не падает сразу. При необходимости: `RAILWAY_CONFLICT_RETRIES`, `RAILWAY_CONFLICT_WAIT_SEC`.

**Один worker с этим токеном:** в Railway не включайте **два сервиса**, которые оба запускают **этого** бота с одним `TELEGRAM_BOT_TOKEN`. Сайт на Vercel сюда не попадает. Если создано два worker'а под одним токеном — **Pause** лишний.

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

1. В Railway: **Pause** / удалить **worker бота** (не путать с сайтом на Vercel).
2. Подождать ~30 с, `scripts\check_telegram_conflict.py` → OK.
3. `.env`: `RUN_MODE=local`
4. `start_bot.bat`

## Конфликт токена

Если `check_telegram_conflict.py` падает: где-то ещё запущен бот (второе окно, второй Railway worker, другой ПК). Оставьте **один** источник polling. Сайт на Vercel к `getUpdates` не обращается.
