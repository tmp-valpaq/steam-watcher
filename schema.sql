CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    steam_api_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    steam_id TEXT NOT NULL,
    name TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL DEFAULT 30,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(telegram_id, steam_id)
);

CREATE TABLE IF NOT EXISTS target_states (
    target_id INTEGER PRIMARY KEY,
    persona_state INTEGER,
    persona_name TEXT,
    game_id TEXT,
    game_name TEXT,
    playtime_forever INTEGER,
    last_logoff INTEGER,
    last_checked INTEGER,
    FOREIGN KEY (target_id) REFERENCES targets(id)
);
