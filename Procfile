web: gunicorn --config gunicorn.conf.py "app:create_app()"
worker: celery -A app.workers.celery_app:celery worker --loglevel=info --concurrency=4 -Q celery,monitor
beat: celery -A app.workers.celery_app:celery beat --loglevel=info