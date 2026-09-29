#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
require_project
mkdir -p artifacts/backups
umask 077
backup_dir="$(mktemp -d "$project_root/artifacts/backups/backup_$(date -u +%Y%m%dT%H%M%SZ)_XXXXXX")"
backup_temp="$backup_dir/database.dump.partial"
trap 'rm -f -- "$backup_temp"' EXIT
compose exec -T postgres sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-acl' > "$backup_temp"
[[ -s "$backup_temp" ]] || fail 'Получен пустой дамп.'
mv -- "$backup_temp" "$backup_dir/database.dump"
printf 'Дамп сохранён: %s\n' "$backup_dir/database.dump"
