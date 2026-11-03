#!/bin/sh
# Start the accounts service: check the database, apply migrations, back it up, then serve.
set -e
python manage.py checkdb
python manage.py migrate --noinput -v 0
python manage.py createcachetable
python manage.py nightly
# No access log here: the website's nginx keeps it (without query strings for /api/).
exec gunicorn fh_accounts.wsgi --bind "${FORZA_ACCOUNTS_BIND:-0.0.0.0:8080}" \
    --workers "${FORZA_ACCOUNTS_WORKERS:-2}" --threads 4 --timeout 30 --graceful-timeout 20 --no-control-socket
