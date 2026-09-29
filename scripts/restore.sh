#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
require_project
[[ "${CONFIRM:-}" == 'restore' ]] || fail 'Нужно CONFIRM=restore: объекты из дампа заменят соответствующие объекты БД.'
require_value FILE "${FILE:-}"
[[ -f "$FILE" && -s "$FILE" && ! -L "$FILE" ]] || fail 'Дамп не найден, пуст или является симлинком.'
# Проверить формат до остановки приложения. LIST не меняет БД.
compose exec -T postgres pg_restore --list < "$FILE" > /dev/null
compose stop web api
compose exec -T postgres sh -c 'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-acl --single-transaction --exit-on-error' < "$FILE"
printf '%s\n' 'Дамп восстановлен. Выполните make migrate, затем make up и make smoke.'
