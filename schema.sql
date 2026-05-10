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
    last_match_id TEXT,
    last_match_time INTEGER,
    FOREIGN KEY (target_id) REFERENCES targets(id)
);

CREATE TABLE IF NOT EXISTS user_settings (
    telegram_id INTEGER PRIMARY KEY,
    daily_summary_enabled INTEGER NOT NULL DEFAULT 1,
    daily_summary_time TEXT NOT NULL DEFAULT '21:00',
    session_updates_enabled INTEGER NOT NULL DEFAULT 1,
    session_update_interval INTEGER NOT NULL DEFAULT 900,
    privacy_alerts_enabled INTEGER NOT NULL DEFAULT 1,
    last_summary_date TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS target_settings (
    target_id INTEGER PRIMARY KEY,
    alert_online INTEGER NOT NULL DEFAULT 1,
    alert_game_start INTEGER NOT NULL DEFAULT 1,
    alert_invisible INTEGER NOT NULL DEFAULT 1,
    alert_privacy INTEGER NOT NULL DEFAULT 1,
    alert_mmr INTEGER NOT NULL DEFAULT 1,
    alert_session INTEGER NOT NULL DEFAULT 1,
    alert_name_change INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (target_id) REFERENCES targets(id)
);

-- Activity log: every detected event with timestamp
-- type: game_start, game_stop, match, online, offline, invisible, name_change
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    game_name TEXT,
    match_id TEXT,
    hero_name TEXT,
    duration_seconds INTEGER,
    details TEXT,
    detected_at INTEGER NOT NULL,
    FOREIGN KEY (target_id) REFERENCES targets(id)
);
CREATE INDEX IF NOT EXISTS idx_activity_log_target ON activity_log(target_id, detected_at DESC);
