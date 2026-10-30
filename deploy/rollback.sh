#!/usr/bin/env bash
# откат бота на прошлую версию. install.sh сам копирует этот скрипт в /opt/tg_assistant/rollback.sh
#
#   sudo /opt/tg_assistant/rollback.sh --check   показать, что будет сделано, ничего не менять
#   sudo /opt/tg_assistant/rollback.sh           вернуть прошлую версию кода, база остаётся текущей
#   sudo /opt/tg_assistant/rollback.sh --db      то же + база на момент перед последним обновлением
set -Eeuo pipefail

APP=tg-assistant
BASE=${BASE:-/opt/tg_assistant}
SVC_USER=${SVC_USER:-tgbot}
PYTHON=${PYTHON:-python3}

MODE=apply WITH_DB=0
while (($#)); do
    case $1 in
        --check) MODE=check ;;
        --db) WITH_DB=1 ;;
        -h | --help) sed -n '4,6p' "$0" | cut -c3-; exit 0 ;;
        *) echo "Неизвестный параметр: $1 (справка: --help)" >&2; exit 2 ;;
    esac
    shift
done

SHARED=$BASE/shared
DB=$SHARED/data/assistant.db
LOG=$SHARED/logs/bot.log
TS=$(date +%Y%m%d-%H%M%S)
STOPPED=0

ok() { printf '  [ok] %s\n' "$*"; }
warn() { printf '  [!]  %s\n' "$*"; }
die() { printf '\nОшибка: %s\n' "$*" >&2; exit 1; }
# если ошибка случилась между остановкой и запуском, бот не должен остаться выключенным
trap 'if ((STOPPED)); then systemctl start "$APP" || true; fi' EXIT

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

wait_started() {
    local offset=$1 size text
    for _ in $(seq 1 45); do
        sleep 1
        size=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
        if ((size < offset)); then offset=0; fi
        text=$(tail -c +"$((offset + 1))" "$LOG" 2>/dev/null || true)
        if grep -q ' is running' <<<"$text"; then
            ok "$(grep -o 'Bot @[^ ]* is running' <<<"$text" | tail -n 1)"
            return 0
        fi
    done
    return 1
}

[[ $EUID -eq 0 ]] || die "запустите через sudo"
[[ -L $BASE/current ]] || die "нет $BASE/current — бот не установлен"
CUR=$(readlink -f "$BASE/current")
PREV=
for dir in "$BASE"/releases/*/; do
    dir=${dir%/}
    if [[ $dir < $CUR ]]; then PREV=$dir; fi
done
[[ -n $PREV ]] || die "нет версии старше $(basename "$CUR") — откатывать некуда"
BAK=$SHARED/backups/assistant-$(basename "$CUR").db

printf '\n== План\n'
echo "  код: $(basename "$CUR") -> $(basename "$PREV"), версия $(basename "$CUR") удалится после успешного запуска"
if ((WITH_DB)); then
    [[ -f $BAK ]] || die "нет бэкапа базы $BAK"
    echo "  база: будет возвращена из $BAK"
    echo "        текущая сохранится в $SHARED/backups/assistant-before-rollback-$TS.db"
else
    echo "  база: остаётся текущей (вернуть и её: --db)"
fi
echo "  сервис $APP перезапустится"
if [[ $MODE == check ]]; then
    printf '\nНичего не изменено. Для отката — та же команда без --check.\n'
    exit 0
fi

printf '\n== Откат\n'
systemctl stop "$APP"
STOPPED=1
ok "сервис остановлен"
if ((WITH_DB)); then
    if [[ -f $DB ]]; then db_copy "$DB" "$SHARED/backups/assistant-before-rollback-$TS.db"; fi
    db_copy "$BAK" "$DB"
    chown -R "$SVC_USER:$SVC_USER" "$SHARED/data"
    ok "база возвращена из $(basename "$BAK")"
fi
ln -sfn "releases/$(basename "$PREV")" "$BASE/current.new"
mv -Tf "$BASE/current.new" "$BASE/current"
systemctl reset-failed "$APP" 2>/dev/null || true
offset=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
systemctl start "$APP"
STOPPED=0
if wait_started "$offset"; then
    # версию, с которой откатились, удаляем. тогда следующий откат пойдет еще на шаг назад
    rm -rf -- "$CUR"
    printf '\nОткат на %s сделан, версия %s удалена.\n' "$(basename "$PREV")" "$(basename "$CUR")"
else
    journalctl -u "$APP" -n 20 --no-pager 2>/dev/null || tail -n 20 "$LOG" 2>/dev/null || true
    die "после отката бот не запустился за 45 с (версия $(basename "$CUR") не удалена): journalctl -u $APP -n 50"
fi
