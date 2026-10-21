# v14+ (Pilot Single-Worker Deployment): uvicorn worker composes RedLab (FastAPI/ASGI) at /redlab/*
# and Brydl Dashboard (Flask/WSGI) at /* through asgi:app. Exactly 1 worker for pilot rate-limit enforcement.
web: gunicorn -k uvicorn.workers.UvicornWorker --workers 1 asgi:app
