FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RAVEWEB_DATA=/data \
    RAVEWEB_MAX_STREAMS=6

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Fail the build early if the app cannot import (syntax error, missing dep).
RUN RAVEWEB_DATA=/tmp/build-check python -c "import app"

RUN mkdir -p /data && useradd -m -u 10001 rave && chown -R rave:rave /data /app
USER rave

EXPOSE 8097
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8097/healthz',timeout=4)"

# gthread workers release the GIL while a stream is proxied, so eight threads
# per worker can serve several listeners plus the JSON API.  --max-requests
# recycles workers periodically to blunt slow leaks.
CMD ["gunicorn", "-w", "2", "-k", "gthread", "--threads", "8", "-t", "120", \
     "--graceful-timeout", "25", "--max-requests", "2000", \
     "--max-requests-jitter", "300", "-b", "0.0.0.0:8097", "app:app"]
