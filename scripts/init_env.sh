#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
require_docker
mkdir -p data/inbox artifacts/backups
if [[ -e .env ]]; then
  printf '%s\n' '.env уже существует, сохранён без изменений.'
  exit 0
fi
[[ -f .env.example ]] || fail 'Нет .env.example.'
env_temp="$(mktemp "$project_root/.env.XXXXXX")"
trap 'rm -f -- "$env_temp"' EXIT
umask 077
docker run --rm -i python:3.13.5-slim-bookworm python -c 'import secrets,sys; text=sys.stdin.read(); marker="__GENERATE_ON_INIT__"; assert marker in text, "Password marker missing"; sys.stdout.write(text.replace(marker,secrets.token_hex(24)))' < .env.example > "$env_temp"
[[ -s "$env_temp" ]] || fail 'Не удалось создать конфигурацию.'
chmod 600 "$env_temp"
ln -- "$env_temp" .env || fail '.env появился параллельно; существующий файл не изменён.'
printf '%s\n' 'Создан .env; пароль БД сгенерирован.'
