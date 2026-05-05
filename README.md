# Steam Watcher

A Telegram bot that monitors Steam profiles and sends you alerts when your tracked players come online, start playing games, go offline, or try to appear invisible.

## Features

- Track multiple Steam profiles
- Real-time status change alerts
- Invisible/offline detection (shows offline but playtime is increasing)
- Game start/stop notifications
- Pause/resume individual targets
- Instant profile check command

## Setup

### Prerequisites

- Python 3.11+
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- A Steam Web API key (from [Steam](https://steamcommunity.com/dev/apikey))

### Install

```bash
cd steam-watcher
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure

Set the environment variable:

```bash
export STEAM_BOT_TOKEN="your_telegram_bot_token"
```

### Run

```bash
python -m src.main
```

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Show help text |
| `/setkey API_KEY` | Set your Steam Web API key (validated) |
| `/add STEAMID64 NAME` | Add a target to monitor |
| `/remove STEAMID64` | Remove a target |
| `/list` | Show all targets with current status |
| `/pause STEAMID64` | Pause monitoring for a target |
| `/resume STEAMID64` | Resume monitoring for a target |
| `/check STEAMID64` | Instant check of a target's profile |

## Usage Flow

1. Start a chat with your bot and send `/start`
2. Set your Steam API key: `/setkey YOUR_KEY`
3. Add targets: `/add 76561198000000001 MyFriend`
4. The bot will now monitor and alert you on changes

## Testing

```bash
pytest tests/ -v
```

## Deployment

### Systemd

Create `/etc/systemd/system/steam-watcher.service`:

```ini
[Unit]
Description=Steam Watcher Telegram Bot
After=network.target

[Service]
Type=simple
User=steambot
WorkingDirectory=/opt/steam-watcher
Environment=STEAM_BOT_TOKEN=your_token
ExecStart=/opt/steam-watcher/.venv/bin/python -m src.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "-m", "src.main"]
```

```bash
docker build -t steam-watcher .
docker run -d --name steam-watcher \
  -e STEAM_BOT_TOKEN=your_token \
  -v steam-watcher-data:/app/data \
  steam-watcher
```

## Project Structure

```
steam-watcher/
├── src/
│   ├── config.py      # Environment config, constants
│   ├── models.py      # Dataclasses
│   ├── steam.py       # Steam API client
│   ├── db.py          # SQLite CRUD
│   ├── watcher.py     # Background polling + alerts
│   ├── bot.py         # Telegram command handlers
│   └── main.py        # Entry point
├── tests/
│   ├── conftest.py    # Test fixtures
│   ├── test_steam.py
│   ├── test_watcher.py
│   └── test_db.py
├── schema.sql
├── requirements.txt
├── ARCHITECTURE.md
└── README.md
```
