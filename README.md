# Steam Watcher

Telegram-бот для мониторинга Steam-профилей. Присылает уведомления когда отслеживаемые игроки заходят онлайн, начинают играть, выходят или пытаются казаться невидимыми.

## Возможности

- Отслеживание нескольких профилей Steam
- Уведомления об изменении статуса в реальном времени
- Детект невидимок (статус offline, но наигранные часы растут)
- Уведомления о начале/конце игры
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

### 2. Узнать SteamID64
Иди на https://steamid.io — вставь ссылку на Steam-профиль человека, получишь 17-значный номер вида `76561198XXXXXXXXX`.

### 3. Настроить бота
В Telegram:
```
/start                          — помощь
/setkey XXXXXXXXXX              — добавить свой Steam API ключ
/add 76561198XXXXXXXXX Вася     — добавить таргет для мониторинга
```

### 4. Пользоваться
Бот автоматически мониторит все добавленные таргеты каждые 30 секунд и присылает уведомления.

## Команды

- `/start` — показать помощь
- `/setkey API_KEY` — добавить Steam API ключ (проверяется валидность)
- `/add STEAMID64 ИМЯ` — добавить таргет
- `/remove STEAMID64` — удалить таргет
- `/list` — список всех таргетов с текущим статусом
- `/pause STEAMID64` — приостановить мониторинг таргета
- `/resume STEAMID64` — возобновить мониторинг
- `/check STEAMID64` — мгновенная проверка профиля

## Важно

- Профиль отслеживаемого игрока должен быть **публичным** (Public). Приватные профили не отдаются через API.
- API ключ привязан к твоему аккаунту Steam. Не делись им.
- Бот проверяет каждые 30 секунд. Невидимка детектится по росту наигранных часов — если `playtime_forever` увеличился, а статус offline, значит человек играет.

## Тесты

```bash
pytest tests/ -v
```

42 теста покрывают: Steam API логику, генерацию алертов, CRUD операции с БД.

## Деплой

### Systemd

Создай `/etc/systemd/system/steam-watcher.service`:

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

```bash
systemctl enable steam-watcher
systemctl start steam-watcher
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
  -e STEAM_BOT_TOKEN=вставь_сюда_токен_от_botfather \
  steam-watcher
```

## Структура проекта

```
steam-watcher/
├── src/
│   ├── config.py      # Конфигурация из env, константы
│   ├── models.py      # Датаклассы
│   ├── steam.py       # Клиент Steam API
│   ├── db.py          # SQLite CRUD
│   ├── watcher.py     # Фоновый поллинг + генерация алертов
│   ├── bot.py         # Telegram обработчики команд
│   └── main.py        # Точка входа
├── tests/
│   ├── conftest.py    # Фикстуры (in-memory DB, моки)
│   ├── test_steam.py
│   ├── test_watcher.py
│   └── test_db.py
├── schema.sql
├── requirements.txt
├── ARCHITECTURE.md
└── README.md
```
