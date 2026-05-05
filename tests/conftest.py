import asyncio
import pytest
import pytest_asyncio
import aiosqlite

import sys
import os

# Ensure src is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db import init_db


@pytest_asyncio.fixture
async def db_conn():
    """Create an in-memory SQLite database with schema initialized."""
    async with aiosqlite.connect(":memory:") as conn:
        await init_db(conn)
        yield conn


@pytest.fixture
def mock_steam_profile():
    """Return a factory for mock SteamProfile objects."""
    from src.models import SteamProfile

    def _make(
        steam_id="76561198000000001",
        persona_name="TestUser",
        persona_state=1,
        game_id=None,
        game_name=None,
        last_logoff=None,
    ):
        return SteamProfile(
            steam_id=steam_id,
            persona_name=persona_name,
            persona_state=persona_state,
            game_id=game_id,
            game_name=game_name,
            last_logoff=last_logoff,
        )

    return _make


@pytest.fixture
def mock_recent_games():
    """Return a factory for mock RecentGames objects."""
    from src.models import RecentGames

    def _make(steam_id="76561198000000001", games=None):
        if games is None:
            games = []
        return RecentGames(steam_id=steam_id, games=games)

    return _make
