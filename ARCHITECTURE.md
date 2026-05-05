# Steam Watcher — Architecture

## Overview

Steam Watcher — single-process Telegram бот для мониторинга Steam-профилей с детектом невидимок.

## Layers

```
┌─────────────────────────────────────────────┐
│  main.py          — entry point, lifecycle   │
│  (session + db + bot + watcher orchestration)│
└──────────┬──────────────┬───────────────────┘
           │              │
     ┌─────▼─────┐  ┌────▼─────┐
     │  bot.py   │  │watcher.py│
     │ (handlers)│  │ (polling)│
     └─────┬─────┘  └────┬─────┘
           │              │
     ┌─────▼──────────────▼─────┐
     │         db.py             │
     │  (SQLite CRUD, aiosqlite) │
     └───────────┬───────────────┘
                 │
     ┌───────────▼───────────┐
     │       steam.py         │
     │ (Steam API, aiohttp)   │
     └────────────────────────┘
```

## Data Flow

1. **Регистрация** — юзер шлёт `/setkey` → bot.py валидирует ключ через Steam API → сохраняет в db.py
2. **Добавление таргета** — `/add` → bot.py проверяет что профиль существует → сохраняет в db.py
3. **Поллинг** — watcher.py каждые 30с забирает все активные таргеты из db.py, для каждого чекает Steam API
4. **Алерты** — watcher.py сравнивает текущий стейт с предыдущим → генерирует алерты → шлёт через bot.send_message
5. **Состояние** — каждый чек обновляет target_state в db.py (persona_state, current_game, playtime_map)

## Invisible Detection

Ключевая фича. Steam позволяет ставить "Невидимку" — статус показывает offline, но наигранные часы продолжают тикать.

Алгоритм:
1. `persona_state == 0` (offline)
2. `playtime_forever` для какой-либо игры увеличился с прошлого чека
3. Оба условия → пользователь играет в невидимке

Ограничение: если юзер только зашёл в игру и ещё не наиграл ни минуты — не детектится. Playtime обновляется раз в ~1 минуту.

## Key Components

### config.py
- `STEAM_BOT_TOKEN` из окружения
- `DB_PATH` — путь к SQLite файлу
- `PERSONA_STATES` — мапа Steam статусов

### models.py
Dataclasses: `User`, `Target`, `TargetState`, `Alert`, `SteamProfile`, `RecentGames`. Чистые данные, без логики.

### steam.py
- `SteamClient` — обёртка над `aiohttp.ClientSession`, rate limit 1 req/sec
- Методы: `get_player_summaries`, `get_recently_played`, `validate_key`
- Pure functions: `state_name`, `format_last_seen`, `detect_invisible`

### db.py
- Один `aiosqlite.Connection`, передаётся через DI
- CRUD для users, targets, target_states
- Схема из `schema.sql`

### watcher.py
- `generate_alerts()` — чистая функция, без I/O, полностью тестируемая
- `Watcher` — async task, поллит таргеты и отправляет алерты
- Alert callback инжектится для тестируемости

### bot.py
- aiogram 3.x router с хендлерами команд
- Все ответы plain text (без parse_mode — avoids crashes)
- Получает `db_conn` и `steam_client` через closure

### main.py
- Создаёт `aiosqlite` connection и `aiohttp.ClientSession`
- Связывает `SteamClient`, `Watcher`, `Bot`, `Dispatcher`
- Управляет lifecycle (startup/shutdown)

## Design Decisions

| Решение | Почему |
|---------|--------|
| Plain text сообщения | Спецсимволы в Steam-никах крашат Telegram API с Markdown/HTML |
| Один DB connection | Single-process, нет смысла в пуле |
| Один HTTP session | aiohttp сессия переиспользуется, нет накладных расходов |
| Rate limit 1 req/s per key | Steam не документирует лимиты, перестраховка |
| generate_alerts() — pure | Легко тестировать без моков I/O |
| DB path в корне проекта | Просто и предсказуемо для single-instance |

## Scalability

Сейчас: single-process, SQLite. Лимит ~100-200 таргетов на инстанс.

Если нужно больше:
- SQLite → PostgreSQL
- Добавить Redis для rate limiting
- Watcher → отдельный worker с очередью
- Bot → webhook вместо polling
