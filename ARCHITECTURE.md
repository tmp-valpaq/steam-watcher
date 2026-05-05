# Steam Watcher — Architecture

## Overview

Steam Watcher is a Telegram bot that monitors Steam profiles and sends alerts when tracked players change status, start/stop games, or go invisible.

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

1. **User registers** via Telegram commands → `bot.py` validates and stores via `db.py`
2. **Watcher polls** every 10s for targets due for a check
3. For each due target: fetch profile (`steam.py`) → compare to previous state (`db.py`) → generate alerts (`watcher.py`)
4. **Alerts** are sent via `bot.send_message` (plain text, no parse mode)

## Key Components

### config.py
- Reads `STEAM_BOT_TOKEN` from environment
- Defines DB path, API base URL, persona state map

### models.py
- Dataclasses: `User`, `Target`, `TargetState`, `Alert`, `SteamProfile`, `RecentGames`
- Pure data, no behavior

### steam.py
- `SteamClient` wraps `aiohttp.ClientSession`
- Rate limited to 1 req/sec
- Methods: `get_player_summaries`, `get_recently_played`, `validate_key`
- Pure functions: `state_name`, `format_last_seen`, `detect_invisible`

### db.py
- Single `aiosqlite.Connection` passed throughout
- CRUD for users, targets, target_states
- Schema initialized from `schema.sql`

### watcher.py
- `generate_alerts()` — pure function, no I/O, fully testable
- `Watcher` class — async task that polls and dispatches alerts
- Alert callback injected for testability

### bot.py
- aiogram 3.x router with command handlers
- All responses are plain text (no parse_mode)
- Receives `db_conn` and `steam_client` via closure

### main.py
- Creates `aiosqlite` connection and `aiohttp.ClientSession`
- Wires up `SteamClient`, `Watcher`, `Bot`, `Dispatcher`
- Manages lifecycle (startup/shutdown)

## Design Decisions

- **Plain text messages**: Avoids crashes from unescaped special characters in Steam names
- **Single DB connection**: Simple, no connection pool complexity
- **Single HTTP session**: Shared across all API calls
- **Rate limiting**: 1 req/sec per API key, enforced in SteamClient
- **Testability**: Alert logic is a pure function separated from I/O
- **Invisible detection**: `persona_state == 0` but `playtime_forever` increasing
