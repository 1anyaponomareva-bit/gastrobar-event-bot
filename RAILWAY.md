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
| `SPORTS_API_KEY` | да — **один ключ** от [dashboard.api-football.com](https://dashboard.api-football.com/) (Football API). То же значение допускается под именами `API_FOOTBALL_KEY`, `FOOTBALL_API_KEY`, `APISPORTS_API_KEY`. Для хоккея/NBA/F1 нужны права подписки. **Квота/лимит** часто даёт HTTP 200 и пустой `response` — в логах ищите `API-SPORTS errors for` или текст `/check`. |
| `EXPECTED_BOT_USERNAME` | `gastrobar_nhatrang_bot` |
| `TIMEZONE` | `Asia/Ho_Chi_Minh` |
| `BAR_OPEN_TIME` / `BAR_CLOSE_TIME` | по желанию |
| `DAILY_POST_HOUR` | `11` |
| `WEEKLY_RADAR_DOW` / `HOUR` / `MINUTE` | `3` / `10` / `40` |
| `RADAR_WEEKLY_MAX` / `RADAR_MIN_WATCHABILITY` | по `.env.example` |
| `GASTROBAR_TV_COUNT` | `2` |

**`RUN_MODE`:** на Railway **не ставьте `local`** — бот уйдёт в «локальный» режим и будет конфликтовать с самим собой. Лучше: `RUN_MODE=railway` или вообще не задавать (авто по `RAILWAY_ENVIRONMENT`).

**Деплой и `TelegramConflict`:** при перезапуске контейнера старый процесс кратко держит `getUpdates`. Бот на Railway **ждёт** (по умолчанию 40×6 с ≈ 4 мин) и не падает сразу. Переменные: `RAILWAY_CONFLICT_RETRIES`, `RAILWAY_CONFLICT_WAIT_SEC`. Опционально перед первым polling: **`RAILWAY_PRE_POLL_DELAY_SEC=12`** — пауза в секундах, чтобы старая реплика отпустила long polling.

**Если конфликт идёт минутами и в логах два разных `BOT_BUILD_ID` подряд** — это почти всегда **два живых процесса с одним токеном**, не «долгий деплой»:

1. **Service → Settings:** **Replicas = 1** (не 2 и не Autoscaling на 2).
2. В проекте Railway должен быть **один** worker с этим `TELEGRAM_BOT_TOKEN`. Старый сервис **`gastrobar`** (репо сайта) **Pause / удали**, если бот переехал на **`gastrobar-event-bot`**.
3. Локально не запускайте `start_bot.bat` с тем же токеном.

Опционально: `GEMINI_MODEL=gemini-2.5-flash`, `DATABASE_PATH=/data/gastrobar_bot.sqlite3` (если подключён Volume, см. ниже).

## 4. Деплой

После `git push` на `main` Railway пересоберёт сервис. В логах должно быть:

- `RUN_MODE=railway`
- `Handlers ready: /events /daily /check`
- `Polling started`

Проверка в Telegram: `/start`, `/events`.

### `TelegramUnauthorizedError` после Revoke в BotFather

Старый токен **сразу** недействителен. Пока в **Variables** не вставлен **новый** токен и сервис не **Redeploy**, в логах будет `Unauthorized` (раньше — бесконечные retry).

1. BotFather → бот → **API Token** → скопировать **новый** токен.
2. **gastrobar-event-bot** → **Variables** → `TELEGRAM_BOT_TOKEN` → вставить **без кавычек**.
3. **Deployments** → **Redeploy** (обязательно — иначе крутится старый контейнер).
4. В логах: `Running bot username: @gastrobar_nhatrang_bot`, без `Unauthorized`.

### Ошибка «verification failed» при рабочем `/gemini_test`

Это **не** обязательно проблема `GEMINI_API_KEY`: тест делает 1–2 вызова, а **афиша недели** гоняет цепочку поиска + **строгую проверку** каждого события. Для **футбола / UFC / части плей-офф NHL·NBA** без совпадения с **API-SPORTS** карточка может отбрасываться. Проверьте логи Railway: `rejected_unverified_event`, `verify_removed`, `Event Radar verify summary`. Убедитесь, что задан **`SPORTS_API_KEY`** и лимиты API-SPORTS/Gemini (free tier: не включайте **`RADAR_MULTI_SHARD`** без платного лимита).

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
