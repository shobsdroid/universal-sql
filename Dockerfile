FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY web/ ./web/
COPY policy.yaml .

EXPOSE 8099
# Honor PORT env var if set (Cloud Run / Fly.io / Render inject it); default to
# 8099 for local dev and docker compose. exec replaces sh so SIGTERM forwards.
CMD ["sh", "-c", "exec uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-8099}"]
