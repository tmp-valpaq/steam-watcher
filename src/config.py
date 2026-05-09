import os

STEAM_BOT_TOKEN = os.environ.get("STEAM_BOT_TOKEN", "")
DEFAULT_STEAM_API_KEY = os.environ.get("DEFAULT_STEAM_API_KEY", "")

# DB_PATH: configurable via env var, defaults to local file or Docker path
DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "steam_watcher.db"),
)

STEAM_API_BASE = "https://api.steampowered.com"
OPENDOTA_BASE = "https://api.opendota.com/api"

DEFAULT_POLL_INTERVAL = 30  # 30 seconds

PERSONA_STATES = {
    0: "Offline",
    1: "Online",
    2: "Busy",
    3: "Away",
    4: "Snooze",
    5: "Looking to Trade",
    6: "Looking to Play",
}

VISIBILITY_STATES = {
    1: "Private",
    2: "Friends Only",
    3: "Public",
}
