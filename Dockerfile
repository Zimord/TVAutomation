# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN useradd --system --uid 10001 --create-home app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
# /app/data holds the SQLite database (named volume); /app/config is bind-mounted read-only.
RUN mkdir -p /app/data /app/config && chown app:app /app/data

USER app
EXPOSE 8000

# /health is admin-protected, so the check authenticates with the container's own ADMIN_TOKEN.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,sys,urllib.request as u; r=u.Request('http://127.0.0.1:8000/health', headers={'Authorization': 'Bearer ' + os.environ['ADMIN_TOKEN']}); sys.exit(0 if u.urlopen(r, timeout=4).status == 200 else 1)"

# Exactly one worker process: the alert queue, worker thread and kill switch are in-process.
# Proxy headers are handled by the app itself (TRUSTED_PROXIES), not by uvicorn.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers", "--no-server-header", "--timeout-graceful-shutdown", "30"]
