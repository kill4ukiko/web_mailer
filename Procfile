release: python -m scripts.apply_sql
web: gunicorn --config gunicorn.conf.py "app:create_app()"
worker: celery -A app.workers.celery_app:celery worker --loglevel=info --concurrency=2 --max-tasks-per-child=100 -Q celery,monitor
beat: celery -A app.workers.celery_app:celery beat --loglevel=info -s /tmp/celerybeat-schedule
