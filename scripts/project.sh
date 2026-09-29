#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
action="${1:-help}"

case "$action" in
  help)
    cat <<'HELP'
Управление прототипом маршрутов (все инструменты внутри Docker):
  make init / doctor / bootstrap       конфигурация, диагностика, первый запуск
  make build / up / dev                сборка, запуск, разработка
  make stop / down / restart / ps      управление контейнерами
  make logs SERVICE=api                логи (SERVICE необязателен)
  make migrate / seed / data-audit     БД и исходные данные
  make import FILE=source.csv          файл из data/inbox
  make import-reference FILE=control.csv DATASET_ID=<uuid>
  make plan DATASET_ID=<uuid>
  make replan PLAN_ID=<uuid> EVENT_FILE=urgent_event.json
  make lint / typecheck / test / e2e / smoke / check
  make benchmark / verify-release
  make backup
  make restore FILE=artifacts/backups/<name>.dump CONFIRM=restore
  make clean CONFIRM=delete-data       удалить данные только этого проекта

Приложение запускается локально через Docker Compose; внешний адрес по умолчанию — http://localhost:${WEB_PORT:-18032}.
HELP
    exit 0
    ;;
  init)
    exec bash scripts/init_env.sh
    ;;
  bootstrap)
    bash scripts/init_env.sh
    require_project
    compose --profile tools build
    compose up -d --wait --wait-timeout 120 postgres
    backend_tool alembic upgrade head
    prepare_test_database
    backend_tool python -m app.cli seed
    compose up -d --wait --wait-timeout 120
    backend_tool python -m app.cli smoke --base-url http://web:8080
    exit 0
    ;;
esac

require_project
case "$action" in
  doctor)
    compose config --quiet
    for source_file in \
      data/source/beeline_business.pdf \
      data/source/vostok_synthetic.csv \
      data/source/vostok_control.csv \
      data/source/vostok_synthetic_utf8.csv \
      data/source/vostok_control_utf8.csv \
      data/source/yugo_vostok_synthetic_utf8.csv \
      data/source/yugo_vostok_control_utf8.csv \
      data/source/yugocenter_synthetic_utf8.csv \
      data/source/yugocenter_control_utf8.csv; do
      [[ -f "$source_file" ]] || fail "Нет исходного файла: $source_file"
    done
    printf '%s\n' 'Docker, Compose, конфигурация и исходные файлы доступны.'
    ;;
  build) compose --profile tools build ;;
  up) compose up -d --wait --wait-timeout 120 ;;
  dev)
    [[ -f compose.dev.yaml ]] || fail 'Нет compose.dev.yaml.'
    docker compose --env-file "$project_root/.env" -f "$project_root/compose.yaml" -f "$project_root/compose.dev.yaml" up -d --build --wait --wait-timeout 120
    ;;
  stop) compose stop ;;
  down) compose down --remove-orphans ;;
  restart) compose restart ;;
  ps) compose ps ;;
  logs)
    if [[ -n "${SERVICE:-}" ]]; then
      case "$SERVICE" in web|api|postgres|frontend_dev) ;; *) fail 'Неизвестный SERVICE.' ;; esac
      compose logs --tail 200 -f "$SERVICE"
    else
      compose logs --tail 200 -f
    fi
    ;;
  migrate)
    backend_tool alembic upgrade head
    backend_test_migrate
    ;;
  seed) backend_tool python -m app.cli seed ;;
  data-audit) backend_tool python -m app.cli data-audit --output /artifacts/data_audit.json ;;
  import)
    selected_file="$(inbox_path "${FILE:-}")"
    backend_tool python -m app.cli import --file "$selected_file"
    ;;
  import-reference)
    require_value DATASET_ID "${DATASET_ID:-}"
    selected_file="$(inbox_path "${FILE:-}")"
    backend_tool python -m app.cli import-reference --file "$selected_file" --dataset-id "$DATASET_ID"
    ;;
  plan)
    require_value DATASET_ID "${DATASET_ID:-}"
    backend_tool python -m app.cli plan --dataset-id "$DATASET_ID"
    ;;
  replan)
    require_value PLAN_ID "${PLAN_ID:-}"
    selected_event="$(inbox_path "${EVENT_FILE:-}")"
    backend_tool python -m app.cli replan --plan-id "$PLAN_ID" --event-file "$selected_event"
    ;;
  benchmark) backend_tool python -m app.cli benchmark --output-dir /artifacts/benchmark ;;
  lint)
    backend_tool ruff check .
    frontend_tool lint
    ;;
  typecheck)
    backend_tool mypy app
    frontend_tool typecheck
    ;;
  test)
    backend_test_migrate
    backend_tool pytest
    frontend_tool test -- --run
    ;;
  e2e) compose --profile tools run --rm --no-deps -T e2e ;;
  smoke) backend_tool python -m app.cli smoke --base-url http://web:8080 ;;
  check)
    for check_action in lint typecheck test e2e smoke; do
      bash scripts/project.sh "$check_action"
    done
    ;;
  verify-release)
    bash scripts/project.sh build
    compose up -d --wait --wait-timeout 120 postgres
    bash scripts/project.sh migrate
    bash scripts/project.sh up
    bash scripts/project.sh check
    bash scripts/project.sh benchmark
    ;;
  backup) exec bash scripts/backup.sh ;;
  restore) exec bash scripts/restore.sh ;;
  clean)
    [[ "${CONFIRM:-}" == 'delete-data' ]] || fail 'Нужно CONFIRM=delete-data: будут удалены volumes текущего проекта.'
    compose --profile tools down --volumes --remove-orphans
    ;;
  *) fail "Неизвестное действие: $action" ;;
esac
