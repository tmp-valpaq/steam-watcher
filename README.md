# Steam Watcher

Telegram-бот для мониторинга Steam-профилей. Присылает уведомления когда отслеживаемые игроки заходят онлайн, начинают играть, выходят или пытаются казаться невидимыми.

## Возможности

- Отслеживание нескольких профилей Steam
- Уведомления об изменении статуса в реальном времени
- Детект невидимок (статус offline, но наигранные часы растут)
- Скрытая активность — детект Dota 2 матчей через OpenDota даже когда игрок offline
- Уведомления о начале/конце игры с длительностью сессии
- Смена ника — отслеживание изменений
- Изменение видимости профиля
- Кнопка "Консистентность" — история событий от бота + последние Dota 2 матчи
- Ежедневная сводка наигранного времени (с поддержкой часовых поясов)
- Периодические обновления сессий ("играет 2ч 15мин")
- Per-target настройки алертов (вкл/выкл каждый тип)
- Пауза/возобновление для каждого таргета
- Мгновенная проверка профиля по команде
- Каждый юзер использует свой Steam API ключ — лимиты не шарятся

## Что нужно

- Python 3.11+
- Токен Telegram бота (от [@BotFather](https://t.me/BotFather))
- Steam Web API ключ (бесплатно, от [Steam](https://steamcommunity.com/dev/apikey))

## Установка

```bash
git clone https://github.com/tmp-valpaq/steam-watcher.git
cd steam-watcher
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Запуск

```bash
export STEAM_BOT_TOKEN="токен_от_botfather"
python -m src.main
```

## Как использовать

### 1. Получить Steam API ключ
Иди на https://steamcommunity.com/dev/apikey — логинься через Steam, в поле "Domain" пиши что угодно (например `localhost`). Ключ бесплатный, получают мгновенно.

### 2. Настроить бота
В Telegram:
```
/start                          — помощь
/setkey XXXXXXXXXX              — добавить свой Steam API ключ
/add https://steamcommunity.com/id/username
```

Можно просто вставить ссылку на профиль или SteamID64 (17 цифр). Ник подтянется автоматически.

### 3. Пользоваться
Бот автоматически мониторит все добавленные таргеты каждые 30 секунд и присылает уведомления.

## Команды

- `/start` — показать помощь
- `/setkey API_KEY` — добавить Steam API ключ (проверяется валидность)
- `/add ссылка_или_SteamID64` — добавить таргет (ник подтянется сам)
- `/remove ссылка_или_SteamID64` — удалить таргет
- `/list` — список всех таргетов с текущим статусом и inline-кнопками
- `/pause ссылка` — приостановить мониторинг таргета
- `/resume ссылка` — возобновить мониторинг
- `/check ссылка` — мгновенная проверка профиля
- `/rename ссылка новый_ник` — переименовать таргет

## Inline-кнопки (в /list)

Каждый таргет имеет панель кнопок:

- ⏸ Пауза / ▶️ Возобновить
- 📊 Консистентность — статус + история событий (из лога бота) + последние Dota 2 матчи с OpenDota
- ⏱ Сессия — текущая игровая сессия и её длительность
- ⚙️ Алерты — per-target настройка уведомлений
- 🗑 Удалить
- 🔍 Проверить

## Настройки (⚙️)

- 📊 Сводка — ежедневная сводка наигранного времени (вкл/выкл)
- 🕐 Время сводки — выбрать из пресетов
- 🌐 Часовой пояс — timezone для корректного времени сводки
- ⏱ Сессии — периодические обновления длительности игры
- 🔒 Приватность — алерты об изменении видимости профиля

## Per-target алерты

Для каждого таргета отдельно:
- 🟢 Онлайн — заход/выход
- 🎮 Игра — начало/конец игры
- 👻 Невидимка — детект скрытой активности
- 🔒 Приватность — изменение видимости профиля
- 📊 MMR Dota — показ ранга при начале игры
- ⏱ Сессии — обновления длительности
- 📝 Смена ника

## Важно

- Профиль отслеживаемого игрока должен быть **публичным** (Public). Приватные профили не отдаются через API.
- API ключ привязан к твоему аккаунту Steam. Не делись им.
- Бот проверяет каждые 30 секунд. Невидимка детектится по росту наигранных часов и через OpenDota для Dota 2.
- Названия игр подтягиваются из Steam API автоматически (не только предзаданные).
- Activity log автоматически чистится (хранение 30 дней).

## Тесты

```bash
pytest tests/ -v
```

62 теста покрывают: Steam API логику, генерацию алертов, CRUD операции с БД, match tracker.

## Деплой

### Docker (рекомендуется)

```bash
docker compose up -d --build
```

### Systemd

```ini
[Unit]
Description=Steam Watcher Telegram Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/steam-watcher
Environment=STEAM_BOT_TOKEN=вставь_сюда_токен_от_botfather
ExecStart=/opt/steam-watcher/.venv/bin/python -m src.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Структура проекта

```
steam-watcher/
├── src/
│   ├── config.py          # Конфигурация из env, константы
│   ├── models.py          # Датаклассы (Target, TargetState, Alert, MatchInfo...)
│   ├── steam.py           # Клиент Steam API (batch, vanity resolve)
│   ├── db.py              # SQLite CRUD + activity_log batch/cleanup
│   ├── watcher.py         # Фоновый поллинг + алерты + кеш игр/матчей
│   ├── match_tracker.py   # OpenDota API клиент с rate limiting
│   ├── bot.py             # Telegram обработчики команд и inline кнопок
│   └── main.py            # Точка входа
├── tests/
│   ├── conftest.py        # Фикстуры (in-memory DB, моки)
│   ├── test_steam.py
│   ├── test_watcher.py
│   ├── test_db.py
│   └── test_match_tracker.py
├── schema.sql             # DDL (users, targets, target_states, user_settings,
│                          #        target_settings, activity_log)
├── requirements.txt
├── ARCHITECTURE.md
└── README.md
```

## Архитектура (кратко)

**Polling loop** (watcher.py): каждые 10 сек проверяет таргеты через Steam API, сравнивает с предыдущим состоянием, генерирует алерты. Отдельно каждые 5 мин — опрос OpenDota для offline-таргетов (детект скрытых Dota 2 матчей).

**Self-tracked playtime**: бот ведёт свои счётчики наигранного времени вместо ненадёжного `GetRecentlyPlayedGames`. Инкремент на poll interval при активной игре.

**Activity log**: каждое событие (online/offline, game start/stop, match, name change, invisible) логируется с таймстемпом. Авточистка 30 дней.

**Кеши**: названия игр (из profile data), OpenDota recent matches, Dota MMR/rank — всё in-memory для скорости.
