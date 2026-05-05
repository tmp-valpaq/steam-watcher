# Steam Watcher Bot

Telegram-бот для мониторинга Steam-профилей. Каждый юзер использует свой API-ключ.

## Установка

```bash
cd /tmp/steam-watcher
pip install -r requirements.txt
```

## Запуск

```bash
STEAM_BOT_TOKEN=<токен_бота> python3 bot.py
```

## Команды бота

```
/start                     — помощь
/setkey <api_key>          — добавить Steam API ключ
/add <steamid64> <имя>     — добавить таргет для мониторинга
/remove <steamid64>        — убрать таргет
/list                      — список таргетов
/pause <steamid64>         — пауза мониторинга
/resume <steamid64>        — возобновить
/check <steamid64>         — ручная проверка профиля
```

## Как получить SteamID64

https://steamid.io — вставляешь ссылку на профиль, получаешь 17-значный ID.

## Как получить Steam API ключ

https://steamcommunity.com/dev/apikey — бесплатно, любой домен (пишешь localhost).

## Уведомления

Бот шлёт алерты когда:
- Таргет зашёл онлайн / оффлайн
- Начал / закончил играть
- Обнаружен невидимка (статус offline, но playtime растёт)

## Данные хранятся в

`steam_watcher.db` (SQLite) — ключи, таргеты, стейт.
