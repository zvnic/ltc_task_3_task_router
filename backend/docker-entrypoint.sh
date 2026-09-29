#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${1:-}" == "uvicorn" ]]; then
  alembic upgrade head
fi

exec "$@"
