# Gastrobar — Telegram-бот афиши спорта

**Этот репозиторий** — только **Telegram-бот** (`gastrobar-event-bot`).  
**Сайт Gastrobar** (другой репозиторий, **Vercel**) сюда **не входит** и на Railway **не деплоится**.

Бот сам собирает спортивные события на неделю, показывает администратору отфильтрованную афишу, по кнопкам генерирует посты через **Google Gemini** и может опубликовать их в группу Gastrobar. Расписание: еженедельная рассылка и ежедневная проверка (**Asia/Ho_Chi_Minh**).

## Стек

- Python 3.10+
- aiogram 3, google-genai, python-dotenv, aiosqlite, APScheduler, httpx

## Установка

```bash
cd GASTROBAR_bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Скопируйте `.env.example` в `.env` и заполните переменные.

## Переменные `.env`

| Переменная | Описание |
|------------|----------|
| `TELEGRAM_BOT_TOKEN` | Токен от [@BotFather](https://t.me/BotFather) |
| `GEMINI_API_KEY` | Ключ API Google AI Studio / Gemini |
| `ADMIN_ID` | Ваш числовой Telegram ID (только этот пользователь управляет ботом) |
| `GASTROBAR_GROUP_ID` | ID группы (часто отрицательное число, например `-100...`) |
| `SPORTS_API_KEY` | Зарезервировано под будущий спортивный API |

Опционально: `DATABASE_PATH`, `GEMINI_MODEL`.

## Где взять `GEMINI_API_KEY`

1. Откройте [Google AI Studio](https://aistudio.google.com/apikey) (или консоль Google Cloud с включённым Gemini API).
2. Создайте API key и вставьте в `GEMINI_API_KEY`.

## Как узнать `ADMIN_ID`

Напишите боту [@userinfobot](https://t.me/userinfobot) или [@getmyid_bot](https://t.me/getmyid_bot) — возьмите поле **Id** (число).

## Как узнать `GASTROBAR_GROUP_ID`

1. Добавьте бота в группу (см. ниже).
2. Временно перешлите любое сообщение из группы боту [@getidsbot](https://t.me/getidsbot) или используйте логи своего бота при получении `message.chat.id` для супергруппы (обычно `-100xxxxxxxxxx`).

## Как добавить бота в группу

1. В Telegram: группа → добавить участников → найти вашего бота.
2. Выдайте право **отправлять сообщения** (для публикации постов).
3. Укажите `GASTROBAR_GROUP_ID` в `.env`.

## Запуск локально

```bash
.venv\Scripts\activate
python main.py
```

Или `start_bot.bat` (Windows). В `.env` держите `RUN_MODE=local`.

**Не запускайте локально, если бот уже на Railway** — один токен, один polling.

## Деплой на Railway (production)

Подробно: **[RAILWAY.md](RAILWAY.md)**.

1. Остановите локальный бот (`scripts\kill_bot.ps1`).
2. Push в `main` на GitHub — Railway пересоберёт worker из `railway.toml`.
3. В Variables задайте токены и ключи (как в `.env.example`). `RUN_MODE` на Railway подставляется автоматически.
4. Проверьте логи: `RUN_MODE=railway`, `Polling started`.

Команды для админа:

- `/start` — приветствие.
- `/week` — собрать события на 7 дней, сохранить в SQLite, показать афишу и кнопки генерации.

## Данные и API спорта

Сейчас `get_week_events()` в `sports_events.py` отдаёт **заглушку** с тестовыми событиями и фильтром «интересного для бара». Позже сюда подключается реальный клиент (API-SPORTS, TheSportsDB и т.д.) с использованием `SPORTS_API_KEY` и, при необходимости, `httpx`.

## SQLite

Файл по умолчанию: `gastrobar_bot.sqlite3`.

Таблицы:

- **events** — поля: `id`, `sport`, `title`, `league`, `date`, `time`, `importance`, `reason`, `created_at` (актуальный список недели перезаписывается при `/week` и при автоматической понедельничной рассылке).
- **drafts** — `id`, `type`, `text`, `status` (`draft` / `published` / `cancelled`), `created_at`.

## Лицензия и поддержка

Проект под вашим контролем; при смене модели Gemini при необходимости обновите `GEMINI_MODEL` в `.env`.
