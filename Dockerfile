FROM python:3.12-slim

WORKDIR /app/server

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ .
COPY extension/ /app/extension/

ENV PYTHONUNBUFFERED=1
EXPOSE 4050

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://127.0.0.1:4050/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "4050", "--proxy-headers", "--forwarded-allow-ips", "*"]
