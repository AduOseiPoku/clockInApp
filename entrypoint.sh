#!/bin/sh
set -e

# If arguments are provided to the container, execute them directly
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

echo "==> Running database migrations..."
python manage.py migrate --noinput

echo "==> Initializing demo user accounts..."
python manage.py setup_demo_accounts

echo "==> Collecting static files..."
python manage.py collectstatic --noinput

echo "==> Starting Gunicorn production server on port ${PORT:-8000}..."
exec gunicorn clockin_project.wsgi:application \
    --bind 0.0.0.0:${PORT:-8000} \
    --workers ${GUNICORN_WORKERS:-3} \
    --threads ${GUNICORN_THREADS:-2} \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
