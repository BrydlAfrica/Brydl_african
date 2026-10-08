# v14+: uvicorn worker composes RedLab (FastAPI/ASGI) at /redlab/*
# and Brydl Dashboard (Flask/WSGI) at /* through asgi:app. The same
# entry point is used on Render (see render.yaml).
web: gunicorn -k uvicorn.workers.UvicornWorker --workers 2 asgi:app
