# refwatch

Бот, который 24/7 следит за X и присылает в Telegram свежие ссылки `claude.ai/referral/…`
(guest pass на 7 дней Claude Pro) с кнопкой «Активировать».

## Как это работает

- Раз в `POLL_INTERVAL` секунд вызывает X API `GET /2/tweets/search/recent` с запросом
  `url:"claude.ai/referral" -is:retweet`.
- Берёт ссылки из развёрнутых URL поста, поэтому посты «напишите в личку» без самой ссылки не приходят.
- Каждая ссылка приходит один раз: отправленные коды хранятся в `state.json` и переживают рестарт.
- После старта смотрит только на посты за последние `LOOKBACK` секунд: более старые ссылки
  почти наверняка уже заняты.
- Активация остаётся ручной: нажмите кнопку в Telegram, будучи залогиненным в claude.ai.
  Автоактивации нет намеренно: условия Anthropic запрещают автоматизированный доступ к claude.ai,
  за это можно потерять аккаунт.

## Что понадобится

1. **X API Bearer Token**: [developer.x.com](https://developer.x.com), создать приложение и пополнить
   кредиты. Тариф pay-per-use, $0.005 за каждый возвращённый пост. Пустые опросы, судя по прайсу,
   бесплатны, но проверьте расход в консоли в первый день.
2. **Telegram-бот**: создать у [@BotFather](https://t.me/BotFather), написать боту `/start`, затем
   открыть `https://api.telegram.org/bot<TOKEN>/getUpdates` и взять `message.chat.id`.

## Запуск

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # заполнить три обязательные переменные
set -a; . ./.env; set +a
.venv/bin/python refwatch.py
```

При старте бот шлёт в Telegram сообщение «refwatch запущен». Если его нет, проверьте токен и chat id.

## Сервис на хосте (systemd)

Склонируйте репозиторий в `/opt/refwatch`, создайте там `.venv` и `.env` как выше, затем:

```bash
sudo cp deploy/refwatch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now refwatch
journalctl -u refwatch -f
```

## Настройки

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `X_BEARER_TOKEN` | обязательна | Bearer Token приложения X |
| `TELEGRAM_BOT_TOKEN` | обязательна | токен бота от @BotFather |
| `TELEGRAM_CHAT_ID` | обязательна | чат, куда слать ссылки |
| `POLL_INTERVAL` | `10` | пауза между опросами, секунды |
| `LOOKBACK` | `120` | насколько старые посты брать при старте, секунды |
| `STATE_FILE` | `state.json` | файл с уже отправленными кодами |
| `QUERY` | `url:"claude.ai/referral" -is:retweet` | поисковый запрос X |

## Тесты

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Важно

- Guest pass срабатывает только на аккаунте, который ни разу не платил за Claude и не брал триал.
  При активации нужна карта, после 7 дней спишется $20/мес, если не отменить.
- Заметная часть постов с такими ссылками продаёт «пассы» или заманивает в Telegram.
  Пассы бесплатные, платить за них не нужно.
