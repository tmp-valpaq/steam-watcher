-- Users and their Steam API keys
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    steam_api_key TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Monitoring targets
CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    steam_id TEXT NOT NULL,
    name TEXT NOT NULL,
    watch_interval INTEGER DEFAULT 30,
    is_active INTEGER DEFAULT 1,
    FOREIGN KEY (tg_id) REFERENCES users(tg_id),
    UNIQUE(tg_id, steam_id)
);

-- State tracking (last known playtime, status)
CREATE TABLE IF NOT EXISTS target_state (
    target_id INTEGER PRIMARY KEY,
    persona_state INTEGER DEFAULT 0,
    current_game TEXT,
    playtime_map TEXT DEFAULT '{}',
    last_alert TEXT,
    FOREIGN KEY (target_id) REFERENCES targets(id) ON DELETE CASCADE
);
