import importlib
import sys


def _reload_config(monkeypatch, **env):
    keys = [
        "STEAM_BOT_TOKEN",
        "DEFAULT_STEAM_API_KEY",
        "ALLOWED_TELEGRAM_IDS",
        "CHECK_MIN_INTERVAL_SEC",
        "DOTABUFF_BROWSER_ENABLED",
        "DOTABUFF_BROWSER_TIMEOUT_MS",
        "DOTABUFF_BROWSER_WAIT_MS",
        "DOTABUFF_BROWSER_SETTLE_TIMEOUT_MS",
        "DOTABUFF_BROWSER_TOTAL_TIMEOUT_MS",
        "DOTABUFF_CACHE_TTL_SEC",
        "DOTABUFF_EMPTY_CACHE_TTL_SEC",
        "DB_PATH",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("src.config", None)
    import src.config
    return importlib.reload(src.config)


def test_invalid_check_min_interval_uses_default(monkeypatch):
    config = _reload_config(monkeypatch, CHECK_MIN_INTERVAL_SEC="oops")
    assert config.CHECK_MIN_INTERVAL_SEC == 3


def test_invalid_dotabuff_int_envs_use_defaults(monkeypatch):
    config = _reload_config(
        monkeypatch,
        DOTABUFF_BROWSER_TIMEOUT_MS="bad",
        DOTABUFF_BROWSER_WAIT_MS="bad",
        DOTABUFF_BROWSER_SETTLE_TIMEOUT_MS="bad",
        DOTABUFF_BROWSER_TOTAL_TIMEOUT_MS="bad",
        DOTABUFF_CACHE_TTL_SEC="bad",
        DOTABUFF_EMPTY_CACHE_TTL_SEC="bad",
    )
    assert config.DOTABUFF_BROWSER_TIMEOUT_MS == 45000
    assert config.DOTABUFF_BROWSER_WAIT_MS == 3000
    assert config.DOTABUFF_BROWSER_SETTLE_TIMEOUT_MS == 25000
    assert config.DOTABUFF_BROWSER_TOTAL_TIMEOUT_MS == 15000
    assert config.DOTABUFF_CACHE_TTL_SEC == 120
    assert config.DOTABUFF_EMPTY_CACHE_TTL_SEC == 30
