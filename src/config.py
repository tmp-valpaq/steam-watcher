import os

STEAM_BOT_TOKEN = os.environ.get("STEAM_BOT_TOKEN", "")

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "steam_watcher.db")

STEAM_API_BASE = "https://api.steampowered.com"

DEFAULT_POLL_INTERVAL = 300  # 5 minutes

# Steam persona states
PERSONA_STATES = {
    0: "Offline",
    1: "Online",
    2: "Busy",
    3: "Away",
    4: "Snooze",
    5: "Looking to Trade",
    6: "Looking to Play",
}
