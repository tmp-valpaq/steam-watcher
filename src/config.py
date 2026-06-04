import os


def _parse_id_set(raw: str) -> set:
    """Parse a comma-separated list of Telegram IDs into a set[int].

    Ignores blanks and non-integer entries. Empty/whitespace input → empty set.
    """
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return ids


STEAM_BOT_TOKEN = os.environ.get("STEAM_BOT_TOKEN", "")
DEFAULT_STEAM_API_KEY = os.environ.get("DEFAULT_STEAM_API_KEY", "")

# Optional allowlist of Telegram user IDs (comma-separated). Empty set = open to
# everyone (current default behavior); when non-empty, only listed IDs may use the bot.
ALLOWED_TELEGRAM_IDS = _parse_id_set(os.environ.get("ALLOWED_TELEGRAM_IDS", ""))

# Minimum seconds between expensive Steam-hitting actions (/check, check buttons,
# add) per user. Anti-flood guard so one user can't stall global polling.
CHECK_MIN_INTERVAL_SEC = int(os.environ.get("CHECK_MIN_INTERVAL_SEC", "3"))

DOTABUFF_BROWSER_ENABLED = os.environ.get("DOTABUFF_BROWSER_ENABLED", "0") == "1"
DOTABUFF_BROWSER_PROFILE_DIR = os.environ.get(
    "DOTABUFF_BROWSER_PROFILE_DIR",
    os.path.join(os.path.dirname(__file__), "..", ".cache", "dotabuff-playwright-profile"),
)
DOTABUFF_BROWSER_OUTPUT_DIR = os.environ.get(
    "DOTABUFF_BROWSER_OUTPUT_DIR",
    os.path.join(os.path.dirname(__file__), "..", ".cache", "dotabuff-playwright-output"),
)
DOTABUFF_BROWSER_TIMEOUT_MS = int(os.environ.get("DOTABUFF_BROWSER_TIMEOUT_MS", "45000"))
DOTABUFF_BROWSER_WAIT_MS = int(os.environ.get("DOTABUFF_BROWSER_WAIT_MS", "3000"))
DOTABUFF_BROWSER_SETTLE_TIMEOUT_MS = int(
    os.environ.get("DOTABUFF_BROWSER_SETTLE_TIMEOUT_MS", "25000")
)
DOTABUFF_BROWSER_TOTAL_TIMEOUT_MS = int(
    os.environ.get("DOTABUFF_BROWSER_TOTAL_TIMEOUT_MS", "15000")
)
DOTABUFF_CACHE_TTL_SEC = int(os.environ.get("DOTABUFF_CACHE_TTL_SEC", "120"))
DOTABUFF_EMPTY_CACHE_TTL_SEC = int(os.environ.get("DOTABUFF_EMPTY_CACHE_TTL_SEC", "30"))

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
