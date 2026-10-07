FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FLASK_APP=wsgi:app FLASK_CONFIG=production PORT=5000 VIPS_CONCURRENCY=2
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libmariadb-dev pkg-config curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh && useradd --system --uid 10001 --create-home medios && chown -R medios:medios /app
USER medios
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=12s --start-period=60s --retries=3 CMD curl --fail http://127.0.0.1:${PORT:-5000}/health || exit 1
ENTRYPOINT ["/app/entrypoint.sh"]
