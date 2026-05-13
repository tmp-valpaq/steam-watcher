import asyncio
import logging
import os
import sys

import aiohttp
import aiosqlite

from .config import DB_PATH, STEAM_BOT_TOKEN
from .db import init_db
from .match_tracker import MatchTracker
from .steam import SteamClient
from .watcher import Watcher
from .bot import setup_bot
from aiogram import Bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    if not STEAM_BOT_TOKEN:
        logger.error("STEAM_BOT_TOKEN environment variable is required")
        sys.exit(1)

    # Resolve DB path
    db_path = os.path.abspath(DB_PATH)
    logger.info("Using database: %s", db_path)

    # Open single DB connection
    async with aiosqlite.connect(db_path) as db_conn:
        await init_db(db_conn)

        # Single HTTP session
        async with aiohttp.ClientSession() as session:
            steam_client = SteamClient(session)
            match_tracker = MatchTracker(session)

            # Create bot
            bot = Bot(token=STEAM_BOT_TOKEN)

            # Alert sender
            async def send_alert(telegram_id: int, message: str) -> None:
                await bot.send_message(chat_id=telegram_id, text=message)

            # Start watcher
            watcher = Watcher(db_conn, steam_client, send_alert, match_tracker)
            await watcher.start()

            # Setup and start bot dispatcher
            dispatcher = setup_bot(bot, db_conn, steam_client, match_tracker, watcher)

            try:
                logger.info("Starting Steam Watcher bot...")
                await dispatcher.start_polling(bot)
            finally:
                await watcher.stop()
                await dispatcher.stop_polling()
                logger.info("Shutdown complete.")


def run() -> None:
    """Entry point for the package."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
