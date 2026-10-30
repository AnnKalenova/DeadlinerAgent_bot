# Деплой на сервер

Нужен Linux с systemd (Ubuntu 22.04+, Debian 12+ и т. п.) и Python 3.10+. Входящие порты не нужны: бот сам забирает сообщения у Telegram (long polling).

Всё делает один скрипт `deploy/install.sh`, и первую установку, и обновления. Сначала запускайте его с `--check`: он проверит сервер, токен и доступ к Telegram и Yandex Cloud и покажет план, ничего не меняя.

## Первая установка

С компьютера скопировать архив с проектом и `.env` на сервер:

```
ssh user@SERVER mkdir -p deploy
scp tg_assistant_deploy.tar.gz путь/к/.env user@SERVER:deploy/
```

На сервере:

```
cd ~/deploy && tar xzf tg_assistant_deploy.tar.gz
sudo bash tg_assistant/deploy/install.sh --env .env --check   # проверки и план, ничего не меняет
sudo bash tg_assistant/deploy/install.sh --env .env           # установка
rm .env    # рабочая копия теперь в /opt/tg_assistant/shared/.env
```

Бот с этим токеном не должен быть запущен где-то ещё: два экземпляра перехватывают сообщения друг у друга. После запуска скрипт это проверяет и предупредит.

Перенести записи из другой копии бота: остановить её, скопировать `data/assistant.db` (и `assistant.db-wal`, если он есть) в `deploy/` на сервере и добавить к установке `--db assistant.db`.

## Обновление

Из git, если сервер видит репозиторий (для закрытого нужен deploy key или токен):

```
git clone <ссылка на репозиторий> ~/tg_assistant    # только в первый раз
cd ~/tg_assistant && git pull
sudo bash deploy/install.sh --check
sudo bash deploy/install.sh
```

Или архивом. На компьютере, в папке, где лежит `tg_assistant`:

```
tar -czf tg_assistant_deploy.tar.gz --exclude=.env --exclude=.venv --exclude=.git --exclude=logs --exclude=__pycache__ --exclude=assistant.db* tg_assistant
scp tg_assistant_deploy.tar.gz user@SERVER:deploy/
```

На сервере:

```
cd ~/deploy && rm -rf tg_assistant && tar xzf tg_assistant_deploy.tar.gz
sudo bash tg_assistant/deploy/install.sh --check
sudo bash tg_assistant/deploy/install.sh
```

`.env` и база на сервере при обновлении остаются. Что делает скрипт:

1. Собирает новую версию в отдельной папке рядом с работающей: копия кода, своё виртуальное окружение, зависимости.
2. Проверяет её через `deploy/check.py`: всё импортируется, `.env` читается, бот понимает проверочную фразу. Если проверка не прошла, новая версия удаляется, а работающая продолжает работать.
3. Останавливает бота, делает бэкап базы, переключает `current` на новую версию и запускает бота.
4. Ждёт в логе строку `Bot @… is running`. Бот молчит секунд 10–20.

## Откат

```
sudo /opt/tg_assistant/rollback.sh --check   # что будет сделано
sudo /opt/tg_assistant/rollback.sh           # прошлая версия кода, база текущая
sudo /opt/tg_assistant/rollback.sh --db      # и база на момент перед обновлением
```

Версия, с которой откатились, после успешного запуска удаляется, так что повторный откат идёт ещё на шаг назад.

## Где что лежит

| Что | Где |
|---|---|
| код текущей версии | `/opt/tg_assistant/current` → `releases/<дата-время>` (хранятся 3 последние) |
| настройки | `/opt/tg_assistant/shared/.env` |
| база | `/opt/tg_assistant/shared/data/assistant.db` |
| логи | `journalctl -u tg-assistant -f` или `/opt/tg_assistant/shared/logs/bot.log` |
| бэкапы базы и `.env` | `/opt/tg_assistant/shared/backups/` (базы — 5 последних) |
| сервис | `systemctl status tg-assistant`, `restart`, `stop` |

Бот работает от системного пользователя `tgbot` и может писать только в базу и логи. Если он упадёт, systemd перезапустит его через 10 секунд, после перезагрузки сервера он стартует сам.

## Если сервер в РФ

С российских серверов api.telegram.org часто недоступен, `install.sh --check` это покажет. Тогда нужен сервер за рубежом или прокси, строка для `.env`:

```
TELEGRAM_PROXY=socks5://127.0.0.1:1080
```

Подходят также `socks5://user:pass@host:port` и `http://host:port`. Если бот уже стоит, добавьте строку в `/opt/tg_assistant/shared/.env` и выполните `sudo systemctl restart tg-assistant`. Через прокси идёт только Telegram, Yandex Cloud — напрямую.

## Удаление

```
sudo systemctl disable --now tg-assistant
sudo rm -rf /opt/tg_assistant /etc/systemd/system/tg-assistant.service
sudo systemctl daemon-reload && sudo userdel tgbot
```
