#!/bin/sh
set -eu
echo "Aplicando migraciones de la sala de medios..."
flask db upgrade
flask seed-data
flask init-storage
(
  while :; do
    flask cleanup-storage || echo "Limpieza pendiente; se reintentará." >&2
    sleep 300
  done
) &
exec gunicorn --config /app/gunicorn.conf.py "wsgi:app"
