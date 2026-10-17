FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INBOX_APP_HOST=0.0.0.0 \
    INBOX_APP_PORT=8000 \
    INBOX_CONFIG_PATH=/app/data/inbox_config.json

WORKDIR /app

RUN addgroup --system inbox && adduser --system --ingroup inbox inbox

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=inbox:inbox scripts ./scripts
COPY --chown=inbox:inbox static ./static

# Compose may run the app as the host UID/GID so the bind-mounted data
# directory remains writable. Keep application code readable for that user.
RUN find /app/scripts -type d -exec chmod 755 {} + \
    && find /app/scripts -type f -exec chmod 644 {} + \
    && find /app/static -type d -exec chmod 755 {} + \
    && find /app/static -type f -exec chmod 644 {} +

RUN mkdir -p /app/data && chown inbox:inbox /app/data

USER inbox
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/', timeout=3).read(1)"

CMD ["python", "-u", "scripts/main.py"]
