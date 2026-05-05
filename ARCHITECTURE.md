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

1. **Добавление таргета** — `/add ссылка` → bot.py резолвит vanity URL → берёт ник из Steam → сохраняет в db.py
2. **Поллинг** — watcher.py каждые 30с забирает все активные таргеты из db.py, для каждого чекает Steam API
3. **Алерты** — watcher.py сравнивает текущий стейт с предыдущим → генерирует алерты → шлёт через bot.send_message
4. **Состояние** — каждый чек обновляет target_state в db.py (persona_state, current_game, playtime)

## Invisible Detection

Ключевая фича. Steam позволяет ставить "Невидимку" — статус показывает offline, но наигранные часы продолжают тикать.

Алгоритм:
1. `persona_state == 0` (offline)
2. `playtime_forever` для какой-либо игры увеличился с прошлого чека
3. Оба условия → пользователь играет в невидимке

Ограничение: если юзер только зашёл в игру и ещё не наиграл ни минуты — не детектится. Playtime обновляется раз в ~1 минуту.

## Third-Party Match Tracking (идея, не реализовано)

### Проблема
Человек может сидеть в невидимке месяцами. Steam API показывает "Last online: 30 дней назад", playtime не растёт (если не играет). Но если он играет в CS2/Dota — его последний матч виден на сторонних сайтах даже при закрытом профиле.

### Как это работает
Сторонние сайты (dotabuff, cstracker, leetify) показывают последний матч через матч-историю. Механика:
- Даже если профиль Private, если в лобби был хотя бы один игрок с публичным профилем — вся катка видна
- Данные цепляются по цепочке: один публичный игрок в матче → раскрывает всех остальных
- Не всегда показывает последний матч — зависит от сайта и игры

### Что можно парсить
- **CS2**: cstracker.gg, leetify — показывают последний матч с датой
- **Dota 2**: dotabuff.com, opendota.com — показывают последний матч с точным временем
- **Общее**: steamid.uk — показывает историю банов, смен ника (даже на приватных)

### Что это даёт
- Последний матч → дата/время → "человек играл 2 часа назад" даже если невидимка
- Работает на Private профилях (не всегда, но часто)
- Можно комбинировать с Steam API для более точного детекта

### Риски
- Парсинг сайтов = хрупко (верстка меняется, можно забанить)
- Работает только для CS2/Dota, не для всех игр
- Некоторые сайты требуют авторизацию / имеют rate limit
- Не всегда показывает именно последний матч

### Потенциальная реализация
1. Добавить `MatchTracker` класс в src/
2. Для каждого таргета хранить `last_match_time` в target_states
3. Парсить dotabuff/cstracker раз в 5 минут
4. Если `last_match_time` новее чем `last_seen` из Steam API → алерт "скрытая активность"
5. Использовать headless browser или API если доступно

## Key Components

### config.py
- `STEAM_BOT_TOKEN` из окружения
- `DEFAULT_STEAM_API_KEY` — серверный API ключ (юзерам не нужен свои)
- `DB_PATH` — путь к SQLite файлу
- `DEFAULT_POLL_INTERVAL` — 30 сек
- `PERSONA_STATES` — мапа Steam статусов

### models.py
Dataclasses: `User`, `Target`, `TargetState`, `Alert`, `SteamProfile`, `RecentGames`. Чистые данные, без логики.

### steam.py
- `SteamClient` — обёртка над `aiohttp.ClientSession`, rate limit 1 req/sec
- Методы: `get_player_summaries`, `get_recently_played`, `resolve_vanity_url`, `validate_key`
- Pure functions: `state_name`, `format_last_seen`, `detect_invisible`

### db.py
- Один `aiosqlite.Connection`, передаётся через DI
- CRUD для users, targets, target_states
- `rename_target` для обновления отображаемого имени

### watcher.py
- `generate_alerts()` — чистая функция, без I/O, полностью тестируемая
- `Watcher` — async task, поллит таргеты и отправляет алерты
- Alert callback инжектится для тестируемости
- Fallback на DEFAULT_STEAM_API_KEY если у юзера нет своего ключа

### bot.py
- aiogram 3.x router с хендлерами команд
- Все ответы plain text (без parse_mode)
- `_parse_steam_input()` — парсит URL / SteamID64 / vanity name
- `_resolve_target_id()` — резолвит любой ввод в SteamID64 (URL → vanity → steam API)
- Команды принимают URL, SteamID64 или имя таргета

### main.py
- Создаёт `aiosqlite` connection и `aiohttp.ClientSession`
- Связывает `SteamClient`, `Watcher`, `Bot`, `Dispatcher`
- Управляет lifecycle (startup/shutdown)

## UX Flow

### Простой путь (без своего API ключа)
1. `/add https://steamcommunity.com/id/gabelogannewell` — всё, ник подтянется сам
2. Бот мониторит и шлёт алерты

### Продвинутый (свой API ключ)
1. `/setkey YOUR_KEY` — привязать свой ключ
2. `/add ссылка` — добавить таргета
3. `/rename ссылка новый_ник` — переименовать если нужно

### Команды
- `/add ссылка` — добавить таргета (ник из Steam)
- `/remove ссылка` — удалить
- `/list` — все таргеты со статусом
- `/pause ссылка` / `/resume ссылка`
- `/check ссылка` — мгновенная проверка
- `/rename ссылка новый_ник` — переименовать
- `/setkey API_KEY` — (опционально) свой ключ

## Design Decisions

| Решение | Почему |
|---------|--------|
| Plain text сообщения | Спецсимволы в Steam-никах крашат Telegram API с Markdown/HTML |
| Один DB connection | Single-process, нет смысла в пуле |
| Один HTTP session | aiohttp сессия переиспользуется |
| Rate limit 1 req/s | Steam не документирует лимиты |
| generate_alerts() — pure | Легко тестировать без моков I/O |
| Дефолтный API ключ | Юзерам не нужно регистрироваться на Steam Dev |
| URL вместо SteamID64 | Простота для обычных юзеров |
| Авто-ник из Steam | Не заставлять придумывать имена |
| 30 сек интервал | Баланс между скоростью и лимитами API |

## Scalability

Сейчас: single-process, SQLite. Лимит ~100-200 таргетов на инстанс.

Если нужно больше:
- SQLite → PostgreSQL
- Добавить Redis для rate limiting
- Watcher → отдельный worker с очередью
- Bot → webhook вместо polling

## Steam API Limits

Valve не публикует точные лимиты. Общепринятая оценка: ~100,000 запросов/день на ключ (~1.15 req/sec). Мы ставим 1 req/sec — безопасно. С дефолтным серверным ключом и 30с интервалом: 1 юзер с 5 таргетами = ~14,400 запросов/день, хватает на ~7 юзеров. При превышении — каждому `/setkey`.
