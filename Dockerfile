FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RAVEWEB_DATA=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data && useradd -m -u 10001 rave && chown -R rave:rave /data /app
USER rave

EXPOSE 8097
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8097/healthz',timeout=4)"

CMD ["gunicorn", "-w", "2", "-k", "gthread", "--threads", "8", "-t", "120", "-b", "0.0.0.0:8097", "app:app"]
