# Steam Watcher — Architecture

## Overview

Steam Watcher — single-process Telegram бот для мониторинга Steam-профилей с детектом невидимок, скрытой активности через OpenDota, и полной историей событий.

## Layers

```
┌─────────────────────────────────────────────┐
│  main.py          — entry point, lifecycle   │
│  (session + db + bot + watcher orchestration)│
└──────────┬──────────────┬───────────────────┘
           │              │
     ┌─────▼─────┐  ┌────▼──────┐
     │  bot.py   │  │ watcher.py│
     │ (handlers)│  │ (polling) │
     └─────┬─────┘  └────┬──────┘
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

     ┌───────────────────────┐
     │   match_tracker.py    │
     │ (OpenDota API client) │
     └───────────────────────┘
```

## Data Flow

1. **Добавление таргета** — `/add ссылка` → bot.py резолвит vanity URL → берёт ник из Steam → сохраняет в db.py
2. **Поллинг** — watcher.py каждые 10с забирает все активные таргеты из db.py, группирует по API ключу, batch-запрос к Steam API
3. **Алерты** — watcher.py сравнивает текущий стейт с предыдущим → генерирует алерты → шлёт через bot.send_message
4. **Состояние** — каждый чек обновляет target_state в db.py (persona_state, current_game, playtimes)
5. **Activity log** — каждое событие логируется в activity_log (batch insert, один commit)
6. **Match polling** — каждые 5 мин OpenDota опрашивается для offline-таргетов (детект скрытых Dota 2 матчей)

## Invisible Detection

Ключевая фича. Steam позволяет ставить "Невидимку" — статус показывает offline, но наигранные часы продолжают тикать.

### Два уровня детекта:

1. **Playtime delta** (основной): `persona_state == 0` и `playtime_forever` для какой-либо игры увеличился. Бот ведёт свои счётчики `game_playtimes` JSON вместо ненадёжного `GetRecentlyPlayedGames`.

2. **OpenDota match check** (для Dota 2): бот опрашивает OpenDota API каждые 5 мин для offline-таргетов. Если найден новый матч — алерт "скрытая активность".

## Self-Tracked Playtime

Бот больше НЕ зависит от `GetRecentlyPlayedGames` (ненадёжный, кешированный, только 2 недели). Вместо этого:

- Хранит `game_playtimes` JSON в target_states — мапа appid → minutes
- При каждом поллинге инкрементит текущую игру на poll interval (30 сек = ~0.5 мин)
- Это даёт стабильные данные для детекта невидимок и daily summary

## Activity Log

Таблица `activity_log` с типами: `game_start`, `game_stop`, `match`, `online`, `offline`, `invisible`, `name_change`.

- Batch insert — все события одного поллинга пишутся одним `executemany`
- Авточистка — раз в день удаляются записи старше 30 дней
- Кнопка "Консистентность" показывает последние 10 событий с иконками

## Caching

In-memory кеши в Watcher для минимизации API-вызовов:

- **Game names** — `game_name_cache: Dict[appid, name]` — пополняется из profile data при каждом поллинге. Не только hardcoded 12 игр.
- **Recent matches** — `recent_matches_cache: Dict[steam_id, list[MatchInfo]]` — кешируется при match polling, переиспользуется в кнопке "Консистентность"
- **Dota MMR/rank** — `opendota_rank_cache` с TTL 10 мин, обогащает алерт "начал играть в Dota 2"

Все кеши in-memory — теряются при рестарте, но быстро заполняются.

## Key Components

### config.py
- `STEAM_BOT_TOKEN` из окружения
- `DEFAULT_STEAM_API_KEY` — серверный API ключ
- `DB_PATH` — путь к SQLite файлу
- `DEFAULT_POLL_INTERVAL` — 30 сек
- `PERSONA_STATES`, `VISIBILITY_STATES` — мапы статусов

### models.py
Dataclasses: `User`, `Target`, `TargetState`, `Alert`, `SteamProfile`, `MatchInfo`, `UserSettings`. Чистые данные, без логики.

### steam.py
- `SteamClient` — обёртка над `aiohttp.ClientSession`, batch API support
- Методы: `get_player_summaries`, `get_player_summaries_batch`, `resolve_vanity_url`, `validate_key`
- Pure functions: `state_name`, `format_last_seen`, `detect_invisible`
- `get_recently_played` УДАЛЁН — заменён на self-tracked playtime

### db.py
- Один `aiosqlite.Connection`, передаётся через DI
- CRUD для users, targets, target_states, user_settings, target_settings
- `get_target_by_id()` — прямой запрос по ID + ownership check (не N+1)
- `save_activity_batch()` — batch insert для activity_log
- `cleanup_activity_log()` — удаление старых записей
- Schema migration: auto-add missing columns при init

### watcher.py
- `generate_alerts()` — чистая функция, без I/O, полностью тестируемая
- `Watcher` — async task, поллит таргеты и отправляет алерты
- Alert callback инжектится для тестируемости
- Fallback на DEFAULT_STEAM_API_KEY если у юзера нет своего ключа
- Периодические задачи: session updates (60с), daily summaries (60с), activity cleanup (1/день)

### match_tracker.py
- `MatchTracker` — OpenDota API client с rate limiting (1 req/sec)
- Hero name cache (lazy load)
- Методы: `get_last_match`, `get_recent_matches`, `get_last_matches_batch`

### bot.py
- aiogram 3.x router с хендлерами команд и inline-кнопок
- Bottom menu: ➕ Добавить, 📋 Мой список, 🔍 Проверить, ⚙️ Настройки
- Inline кнопки на таргетах: Пауза, Консистентность, Сессия, Алерты, Удалить, Проверить
- Settings panel: сводка, время сводки, timezone, сессии, приватность
- Per-target alert settings: 7 типов алертов вкл/выкл
- `_parse_steam_input()` — парсит URL / SteamID64 / vanity name
- `_resolve_target_id()` — резолвит любой ввод в SteamID64

### main.py
- Создаёт `aiosqlite` connection и `aiohttp.ClientSession`
- Связывает `SteamClient`, `MatchTracker`, `Watcher`, `Bot`, `Dispatcher`
- Передаёт `watcher` в `setup_bot()` для доступа к кешу
- Управляет lifecycle (startup/shutdown)

## Timezone Support

- `user_settings.timezone` — default UTC, backward compatible
- Timezone picker в настройках (8 популярных зон)
- Daily summary считает время в timezone пользователя через stdlib `zoneinfo`
- Без внешних зависимостей

## Design Decisions

- **Plain text сообщения** — спецсимволы в Steam-никах крашат Telegram API с Markdown/HTML
- **Один DB connection** — single-process, нет смысла в пуле
- **Один HTTP session** — aiohttp сессия переиспользуется между SteamClient, MatchTracker, и Dota enrichment
- **Rate limit 1 req/s** — Steam не документирует лимиты, OpenDota free tier = 1 req/s
- **generate_alerts() pure** — легко тестировать без моков I/O
- **Self-tracked playtime** — GetRecentlyPlayedGames ненадёжный (кешированный, 2 недели). Свои данные стабильнее
- **Batch activity insert** — 1 commit на все события поллинга вместо N
- **In-memory caches** — теряются при рестарте, но заполняются за 1-2 цикла поллинга
- **30-day activity retention** — автоочистка, таблица не растёт бесконечно
- **URL вместо SteamID64** — простота для обычных юзеров
- **Авто-ник из Steam** — не заставлять придумывать имена
- **30 сек интервал** — баланс между скоростью и лимитами API

## Scalability

Сейчас: single-process, SQLite. Лимит ~100-200 таргетов на инстанс.

Если нужно больше:
- SQLite → PostgreSQL
- Добавить Redis для rate limiting
- Watcher → отдельный worker с очередью
- Bot → webhook вместо polling

## Steam API Limits

Valve не публикует точные лимиты. Общепринятая оценка: ~100,000 запросов/день на ключ (~1.15 req/sec). Мы ставим 1 req/sec — безопасно. Batch API (`get_player_summaries_batch`) уменьшает кол-во запросов. С дефолтным серверным ключом и 30с интервалом: 1 юзер с 5 таргетами = ~14,400 запросов/день, хватает на ~7 юзеров. При превышении — каждому `/setkey`.
