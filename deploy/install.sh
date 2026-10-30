#!/usr/bin/env bash
# установка и обновление бота на linux сервере (systemd + venv). один и тот же скрипт для первого раза и для обновлений
# справка: deploy/install.sh --help
set -Eeuo pipefail

usage() {
    cat <<'EOF'
Установка и обновление бота на сервере (systemd + venv). Запускать через sudo.

  sudo bash tg_assistant/deploy/install.sh --env .env --check   проверки и план, ничего не меняет
  sudo bash tg_assistant/deploy/install.sh --env .env           установить или обновить

  --env FILE  .env с токеном и ключами. Нужен при первой установке. При обновлении
              без --env остаётся серверный .env, с --env он заменяется (старый — в бэкап).
  --db FILE   перенести базу assistant.db с компа (assistant.db-wal, если есть, — рядом).
  --check     только проверить и показать план.

Новая версия собирается рядом с работающей и включается, только если прошла проверка (deploy/check.py).
Перед переключением делается бэкап базы. Откат: sudo /opt/tg_assistant/rollback.sh --check
EOF
}

APP=tg-assistant
BASE=${BASE:-/opt/tg_assistant}
SVC_USER=${SVC_USER:-tgbot}
UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
PYTHON=${PYTHON:-python3}
KEEP_RELEASES=3
KEEP_DB_BACKUPS=5

MODE=apply ENV_ARG='' DB_ARG=''
while (($#)); do
    case $1 in
        --check) MODE=check ;;
        --env) ENV_ARG=${2:?после --env нужен путь к файлу}; shift ;;
        --db) DB_ARG=${2:?после --db нужен путь к файлу}; shift ;;
        -h | --help) usage; exit 0 ;;
        *) echo "Неизвестный параметр: $1 (справка: --help)" >&2; exit 2 ;;
    esac
    shift
done

SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SHARED=$BASE/shared
RELEASES=$BASE/releases
LIVE_ENV=$SHARED/.env
DB=$SHARED/data/assistant.db
LOG=$SHARED/logs/bot.log
TS=$(date +%Y%m%d-%H%M%S)
NEW=$RELEASES/$TS
TMP=$(mktemp -d)
ERRORS=0 WARNINGS=0 BUILDING=0 STOPPED=0 SWITCHED=0

ok() { printf '  [ok] %s\n' "$*"; }
warn() { printf '  [!]  %s\n' "$*"; WARNINGS=$((WARNINGS + 1)); }
fail() { printf '  [x]  %s\n' "$*"; ERRORS=$((ERRORS + 1)); }
step() { printf '\n== %s\n' "$*"; }
die() { printf '\nОшибка: %s\n' "$*" >&2; exit 1; }

# если что-то упало до переключения: недособранную версию удаляем, а старую, если успели остановить, запускаем обратно
cleanup() {
    local rc=$?
    rm -rf "$TMP"
    if ((rc == 0 || SWITCHED)); then return 0; fi
    if ((BUILDING)); then
        rm -rf -- "$NEW"
        echo "Новая версия $TS удалена, рабочая не тронута." >&2
    fi
    if ((STOPPED)); then
        systemctl start "$APP" || true
        echo "Прежняя версия снова запущена." >&2
    fi
    return 0
}
trap cleanup EXIT

# достает значение KEY из .env: последняя строка KEY=..., без кавычек, \r, комментария и пробелов по краям
env_get() {
    local value
    value=$(grep -E "^[[:space:]]*(export[[:space:]]+)?$2[[:space:]]*=" "$1" | tail -n 1 | cut -d= -f2- | tr -d '\r' || true)
    value=${value#"${value%%[![:space:]]*}"}
    if [[ $value != [\"\']* ]]; then value=${value%%[[:space:]]#*}; fi
    value=${value%"${value##*[![:space:]]}"}
    if [[ ${#value} -ge 2 && ($value == \"*\" || $value == \'*\') ]]; then value=${value:1:${#value}-2}; fi
    printf '%s' "$value"
}

# копия базы через backup api sqlite: подхватывает и WAL, база получается целиком в одном файле
db_copy() {
    "$PYTHON" -I - "$1" "$2" <<'EOF'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()
EOF
}

# ждет в логе строку "Bot @... is running" (значит телега ответила), потом еще 15 сек смотрит, нет ли конфликта
wait_started() {
    local offset=$1 size text
    for _ in $(seq 1 45); do
        sleep 1
        size=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
        if ((size < offset)); then offset=0; fi
        text=$(tail -c +"$((offset + 1))" "$LOG" 2>/dev/null || true)
        if grep -q ' is running' <<<"$text"; then
            ok "$(grep -o 'Bot @[^ ]* is running' <<<"$text" | tail -n 1)"
            sleep 15
            text=$(tail -c +"$((offset + 1))" "$LOG" 2>/dev/null || true)
            if grep -q 'TelegramConflictError' <<<"$text"; then
                warn "этот же бот запущен где-то ещё (кто-то запустил с тем же токеном у себя?) — остановите его, иначе сообщения будут теряться"
            fi
            return 0
        fi
    done
    return 1
}

prune() {
    local current dirs i n=0 dir
    current=$(readlink -f "$BASE/current")
    dirs=("$RELEASES"/*/)
    for ((i = ${#dirs[@]} - 1; i >= 0; i--)); do
        dir=${dirs[i]%/}
        n=$((n + 1))
        if ((n > KEEP_RELEASES)) && [[ $dir != "$current" ]]; then rm -rf -- "$dir"; fi
    done
    find "$SHARED/backups" -maxdepth 1 -name 'assistant-2*.db' -printf '%f\n' | sort -r |
        tail -n +$((KEEP_DB_BACKUPS + 1)) | while read -r name; do rm -f -- "$SHARED/backups/$name"; done
}

# ---------------------------------------------------------------- проверки
step "Проверки"
[[ $EUID -eq 0 ]] || die "запустите через sudo"
[[ $BASE == /?* ]] || die "BASE должен быть абсолютным путём"
[[ -f $SRC/main.py && -f $SRC/requirements.txt && -f $SRC/deploy/$APP.service ]] ||
    die "рядом со скриптом нет проекта ($SRC)"

if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
    ok "systemd есть"
else
    fail "нет systemd — скрипт рассчитан на него"
fi

NEED_VENV_PKG=0 PY_VER=
if PY_VER=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null); then
    PY_PATH=$(readlink -f "$(command -v "$PYTHON")")
    if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
        fail "Python $PY_VER, нужен 3.10+ (другой можно указать так: sudo PYTHON=python3.12 ...)"
    elif [[ $PY_PATH == /root/* || $PY_PATH == /home/* ]]; then
        fail "Python лежит в домашней папке ($PY_PATH), сервис его не увидит — нужен системный"
    else
        ok "Python $PY_VER ($PY_PATH)"
    fi
    if "$PYTHON" -c 'import ensurepip, venv' 2>/dev/null; then
        ok "модуль venv есть"
    elif command -v apt-get >/dev/null; then
        NEED_VENV_PKG=1
        warn "нет python$PY_VER-venv — будет установлен через apt-get"
    else
        fail "нет модуля venv (ensurepip) — поставьте пакет python3-venv или аналог"
    fi
    if ! "$PYTHON" -c 'import sqlite3' 2>/dev/null; then fail "в Python нет модуля sqlite3"; fi
else
    fail "не найден $PYTHON"
fi

ENV_ACTION=keep ENV_SRC=$LIVE_ENV
if [[ -n $ENV_ARG ]]; then
    if [[ ! -f $ENV_ARG ]]; then
        fail "нет файла $ENV_ARG"
        ENV_SRC=
    elif [[ ! -f $LIVE_ENV ]]; then
        ENV_ACTION=install ENV_SRC=$ENV_ARG
    elif ! diff -q <(tr -d '\r' <"$ENV_ARG") "$LIVE_ENV" >/dev/null; then
        ENV_ACTION=replace ENV_SRC=$ENV_ARG
    fi
elif [[ ! -f $LIVE_ENV ]]; then
    fail "нет .env: при первой установке укажите --env FILE"
    ENV_SRC=
fi

TOKEN='' PROXY=''
if [[ -n $ENV_SRC ]]; then
    TOKEN=$(env_get "$ENV_SRC" TELEGRAM_BOT_TOKEN)
    PROXY=$(env_get "$ENV_SRC" TELEGRAM_PROXY)
    if [[ $TOKEN =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then
        ok "TELEGRAM_BOT_TOKEN задан"
    else
        fail "TELEGRAM_BOT_TOKEN пустой или не похож на токен"
    fi
    if [[ -n $(env_get "$ENV_SRC" YANDEX_API_KEY) && -n $(env_get "$ENV_SRC" YANDEX_FOLDER_ID) ]]; then
        ok "ключи Yandex Cloud заданы"
    else
        warn "нет ключей Yandex Cloud — бот будет без голоса и поиска по смыслу"
    fi
    if [[ -n $PROXY ]]; then
        if [[ $PROXY =~ ^(socks5|socks4|http)://([^@/]+@)?[^@/:]+:[0-9]+/?$ ]]; then
            ok "TELEGRAM_PROXY задан"
        else
            fail "TELEGRAM_PROXY: нужен вид socks5://host:port, socks5://user:pass@host:port или http://host:port"
        fi
    fi
fi

if [[ -n $DB_ARG ]]; then
    if [[ ! -f $DB_ARG ]]; then
        fail "нет файла $DB_ARG"
    elif db_copy "$DB_ARG" "$TMP/import.db" 2>"$TMP/err" &&
        ITEMS=$("$PYTHON" -I -c 'import sqlite3, sys; print(sqlite3.connect(sys.argv[1]).execute("select count(*) from items").fetchone()[0])' "$TMP/import.db" 2>>"$TMP/err"); then
        ok "база $DB_ARG читается, записей: $ITEMS"
    else
        fail "$DB_ARG не похож на базу бота: $(head -c 160 "$TMP/err" | tr '\n' ' ')"
    fi
fi

if command -v curl >/dev/null; then
    if [[ -n $TOKEN ]]; then
        args=(-sS -m 15 -o "$TMP/getme" -w '%{http_code}')
        if [[ -n $PROXY ]]; then args+=(--proxy "$PROXY"); fi
        # токен отдаем curl через stdin (-K -), чтобы он не светился в списке процессов
        code=$(printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$TOKEN" | curl "${args[@]}" -K - 2>"$TMP/err") || code=000
        case $code in
            200) ok "api.telegram.org доступен${PROXY:+ через прокси}, бот @$(grep -o '"username":"[^"]*"' "$TMP/getme" | cut -d'"' -f4)" ;;
            401 | 404) fail "Telegram не принял токен (HTTP $code) — проверьте TELEGRAM_BOT_TOKEN" ;;
            000)
                fail "api.telegram.org недоступен${PROXY:+ через прокси}: $(head -c 160 "$TMP/err" | tr '\n' ' ')"
                if [[ -z $PROXY ]]; then echo "       из РФ его часто режут — нужен TELEGRAM_PROXY в .env или сервер за рубежом"; fi
                ;;
            *) fail "api.telegram.org ответил HTTP $code" ;;
        esac
    fi
    for host in llm.api.cloud.yandex.net stt.api.cloud.yandex.net tts.api.cloud.yandex.net; do
        code=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' "https://$host/" 2>/dev/null) || code=000
        if [[ $code != 000 ]]; then ok "$host доступен"; else warn "$host недоступен — модель или голос работать не будут"; fi
    done
    code=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' https://pypi.org/simple/aiogram/ 2>/dev/null) || code=000
    if [[ $code == 200 ]]; then ok "pypi.org доступен"; else warn "pypi.org недоступен (HTTP $code) — pip может не скачать зависимости"; fi
else
    warn "нет curl — сеть не проверялась"
fi

# ---------------------------------------------------------------- план
step "План"
CUR_REL=
if [[ -L $BASE/current ]]; then CUR_REL=$(readlink -f "$BASE/current"); fi
if [[ -n $CUR_REL ]]; then
    echo "  обновление: версия $(basename "$CUR_REL") -> $TS, прежняя останется для отката"
    echo "  сервис $APP перезапустится: бот замолчит секунд на 10-20"
else
    echo "  первая установка в $BASE: пользователь $SVC_USER, сервис $APP (автозапуск)"
fi
if ((NEED_VENV_PKG)); then echo "  apt-get install python$PY_VER-venv"; fi
case $ENV_ACTION in
    install) echo "  .env: $ENV_ARG -> $LIVE_ENV" ;;
    replace) echo "  .env: серверный будет заменён на $ENV_ARG (прежний — в $SHARED/backups/)" ;;
    *) if [[ -f $LIVE_ENV ]]; then echo "  .env: остаётся серверный"; fi ;;
esac
if [[ -f $DB ]]; then echo "  бэкап базы перед переключением: $SHARED/backups/assistant-$TS.db"; fi
if [[ -n $DB_ARG ]]; then
    if [[ -f $DB ]]; then echo "  база: серверная будет заменена на $DB_ARG"; else echo "  база: будет перенесена из $DB_ARG"; fi
fi

if ((ERRORS)); then die "проверки не прошли (ошибок: $ERRORS), ничего не изменено"; fi
if [[ $MODE == check ]]; then
    msg="Проверки прошли"
    if ((WARNINGS)); then msg+=" (предупреждений: $WARNINGS)"; fi
    printf '\n%s. Ничего не изменено. Для установки — та же команда без --check.\n' "$msg"
    exit 0
fi

# ---------------------------------------------------------------- сборка новой версии рядом с работающей
step "Сборка версии $TS"
if ((NEED_VENV_PKG)); then
    pkg=python$PY_VER-venv
    if ! { apt-get install -y -q "$pkg" || { apt-get update -q && apt-get install -y -q "$pkg"; }; } >"$TMP/apt.log" 2>&1; then
        tail -n 5 "$TMP/apt.log"
        die "не поставился $pkg"
    fi
    ok "поставлен $pkg"
fi
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$BASE" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
    ok "создан пользователь $SVC_USER"
fi
install -d -m 755 "$BASE" "$RELEASES" "$SHARED"
install -d -m 750 -o "$SVC_USER" -g "$SVC_USER" "$SHARED/data" "$SHARED/logs"
install -d -m 700 "$SHARED/backups"

mkdir "$NEW" 2>/dev/null || die "версия $TS уже есть — запустите ещё раз через секунду"
BUILDING=1
tar -C "$SRC" --exclude=./.env --exclude=./.venv --exclude=./.git --exclude=./logs --exclude='./data/*.db*' \
    --exclude=__pycache__ --exclude='*.pyc' -cf - . | tar -C "$NEW" --no-same-owner -xf -
chmod -R go-w "$NEW"
"$PYTHON" -m venv "$NEW/.venv"
if ! "$NEW/.venv/bin/python" -m pip install -q --disable-pip-version-check -r "$NEW/requirements.txt" >"$TMP/pip.log" 2>&1; then
    tail -n 15 "$TMP/pip.log"
    die "pip install не прошёл"
fi
ok "зависимости установлены ($("$NEW/.venv/bin/python" -m pip list 2>/dev/null | awk '$1 == "aiogram" {print "aiogram " $2}'))"
# быстрая проверка новой версии: все импортируется, .env читается, бот понимает фразу. сеть не нужна
if "$NEW/.venv/bin/python" "$NEW/deploy/check.py" "$ENV_SRC" >"$TMP/check.log" 2>&1; then
    ok "проверка новой версии прошла"
else
    tail -n 25 "$TMP/check.log"
    die "проверка новой версии не прошла (deploy/check.py)"
fi
ln -s ../../shared/.env "$NEW/.env"

sed -e "s|@BASE@|$BASE|g" -e "s|@USER@|$SVC_USER|g" "$SRC/deploy/$APP.service" >"$TMP/unit"
install -m 644 "$TMP/unit" "$UNIT_DIR/$APP.service"
sed -e "s|^BASE=.*|BASE=\${BASE:-$BASE}|" "$SRC/deploy/rollback.sh" >"$TMP/rollback"
install -m 755 "$TMP/rollback" "$BASE/rollback.sh"
systemctl daemon-reload
ok "юнит $UNIT_DIR/$APP.service и $BASE/rollback.sh на месте"

# ---------------------------------------------------------------- переключение
step "Переключение"
state=$(systemctl is-active "$APP" 2>/dev/null || true)
if [[ $state == active || $state == activating || $state == reloading ]]; then
    systemctl stop "$APP"
    STOPPED=1
    ok "прежняя версия остановлена"
fi
if [[ -f $DB ]]; then
    db_copy "$DB" "$SHARED/backups/assistant-$TS.db"
    ok "бэкап базы: $SHARED/backups/assistant-$TS.db"
fi
if [[ -n $DB_ARG ]]; then
    db_copy "$TMP/import.db" "$DB"
    ok "база перенесена из $DB_ARG"
fi
chown -R "$SVC_USER:$SVC_USER" "$SHARED/data" "$SHARED/logs"
if [[ $ENV_ACTION == install || $ENV_ACTION == replace ]]; then
    if [[ $ENV_ACTION == replace ]]; then cp -p "$LIVE_ENV" "$SHARED/backups/env-$TS"; fi
    tr -d '\r' <"$ENV_ARG" >"$TMP/env"
    install -m 640 -o root -g "$SVC_USER" "$TMP/env" "$LIVE_ENV"
    if [[ $ENV_ACTION == replace ]]; then ok ".env заменён (прежний: $SHARED/backups/env-$TS)"; else ok ".env установлен"; fi
fi

ln -sfn "releases/$TS" "$BASE/current.new"
mv -Tf "$BASE/current.new" "$BASE/current"
SWITCHED=1 BUILDING=0
systemctl enable --quiet "$APP"
systemctl reset-failed "$APP" 2>/dev/null || true
offset=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
systemctl restart "$APP"
STOPPED=0
ok "запущена версия $TS, жду ответа Telegram"

if ! wait_started "$offset"; then
    echo
    systemctl --no-pager status "$APP" 2>&1 | head -n 12 || true
    journalctl -u "$APP" -n 20 --no-pager 2>/dev/null || true
    echo "--- новые строки $LOG:"
    tail -c +"$((offset + 1))" "$LOG" 2>/dev/null | tail -n 20 || true
    if [[ -n $CUR_REL ]]; then
        die "бот не запустился за 45 с. Логи выше; откат: sudo $BASE/rollback.sh"
    fi
    die "бот не запустился за 45 с. Логи выше, полностью: journalctl -u $APP -n 100"
fi
prune

step "Готово: версия $TS"
cat <<EOF
  статус:  systemctl status $APP
  логи:    journalctl -u $APP -f    или    tail -f $LOG
  откат:   sudo $BASE/rollback.sh --check
EOF
