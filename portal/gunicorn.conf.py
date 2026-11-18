# Wordt automatisch door gunicorn gelezen (werkmap /app).
# Threads i.p.v. één sync-worker: open/lege browserverbindingen blokkeren dan niet alles.
worker_class = "gthread"
workers = 1
threads = 8
timeout = 60
keepalive = 5
