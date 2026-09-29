#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$project_root"

fail() {
  printf '%s\n' "Ошибка: $*" >&2
  exit 1
}

require_docker() {
  command -v docker >/dev/null 2>&1 || fail 'Установите Docker с Compose v2.'
  docker info >/dev/null 2>&1 || fail 'Docker daemon недоступен.'
  local compose_version major minor
  compose_version="$(docker compose version --short)" || fail 'Docker Compose v2 недоступен.'
  compose_version="${compose_version#v}"
  [[ "$compose_version" =~ ^([0-9]+)\.([0-9]+) ]] || fail 'Не удалось определить версию Compose.'
  major="${BASH_REMATCH[1]}"
  minor="${BASH_REMATCH[2]}"
  (( major > 2 || (major == 2 && minor >= 20) )) || fail 'Требуется Docker Compose 2.20 или новее.'
}

require_project() {
  require_docker
  [[ -f compose.yaml ]] || fail 'compose.yaml не найден. Проверьте целостность репозитория.'
  [[ -f .env ]] || fail 'Нет .env. Выполните make init.'
}

compose() {
  docker compose --env-file "$project_root/.env" -f "$project_root/compose.yaml" "$@"
}

backend_tool() {
  compose --profile tools run --rm --no-deps -T backend_tools uv run --frozen "$@"
}

backend_test_migrate() {
  backend_tool python scripts/migrate_test_db.py
}

frontend_tool() {
  compose --profile tools run --rm --no-deps -T frontend_tools npm run "$@"
}

test_database_name() {
  local url name
  url="$(sed -n 's/^TEST_DATABASE_URL=//p' .env | head -n 1)"
  url="${url%\"}"
  url="${url#\"}"
  url="${url%%\?*}"
  name="${url##*/}"
  [[ "$name" =~ ^[A-Za-z0-9_]+$ ]] || return 1
  printf '%s\n' "$name"
}

# Идемпотентно: создаёт базу для pytest и накатывает на неё миграции.
prepare_test_database() {
  local name
  name="$(test_database_name)" || fail 'В .env нет пригодного TEST_DATABASE_URL.'
  compose exec -T postgres sh -c "psql -U \"\$POSTGRES_USER\" -d postgres -tAc \"SELECT 1 FROM pg_database WHERE datname = '$name'\" | grep -q 1 || createdb -U \"\$POSTGRES_USER\" '$name'"
  compose --profile tools run --rm --no-deps -T backend_tools \
    sh -c 'DATABASE_URL="$TEST_DATABASE_URL" uv run --frozen alembic upgrade head'
  printf '%s\n' "Тестовая база $name готова."
}

require_value() {
  [[ -n "$2" ]] || fail "Требуется параметр $1. См. make help."
}

inbox_path() {
  local file_name="$1"
  [[ -n "$file_name" && "$file_name" != */* && "$file_name" != '.' && "$file_name" != '..' ]] || fail 'FILE должен быть именем файла внутри data/inbox.'
  [[ -f "data/inbox/$file_name" && ! -L "data/inbox/$file_name" ]] || fail 'Файл не найден в data/inbox или является симлинком.'
  printf '/data/inbox/%s\n' "$file_name"
}
