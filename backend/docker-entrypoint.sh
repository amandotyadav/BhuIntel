#!/bin/sh
set -e
# Apply migrations before starting when requested (the api service does; workers do not).
if [ "${BHU_RUN_MIGRATIONS:-0}" = "1" ]; then
  alembic upgrade head
fi
exec "$@"
